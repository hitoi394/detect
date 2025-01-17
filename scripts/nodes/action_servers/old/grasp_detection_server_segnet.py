#!/usr/bin/env python3
from multiprocessing import Pool
from time import time
from typing import List

import cv2
import numpy as np
import rospy
from actionlib import SimpleActionServer
from cv_bridge import CvBridge
from detect.msg import (Candidate, Candidates, DetectedObject,
                        GraspDetectionAction, GraspDetectionDebugInfo,
                        GraspDetectionGoal, GraspDetectionResult, PointTuple2D)
from geometry_msgs.msg import Point, Pose, PoseStamped
from modules.grasp_segnet import GraspDetector
from modules.image import extract_flont_mask_with_thresh, extract_flont_instance_indexes, merge_mask
from modules.visualize import convert_rgb_to_3dgray
from modules.ros.action_clients import (ComputeDepthThresholdClient,
                                        InstanceSegmentationClient, TFClient,
                                        VisualizeClient)
from modules.ros.msg_handlers import RotatedBoundingBoxHandler
from modules.ros.utils import PointProjector, PoseEstimator, multiarray2numpy
from sensor_msgs.msg import CameraInfo
from std_msgs.msg import Header


class GraspDetectionServer:
    def __init__(self, name: str, finger_num: int, hand_radius_mm: int, finger_radius_mm: int,
                 hand_mount_rotation: int, approach_coef: float,
                 info_topic: str, enable_depth_filter: bool,
                 debug: bool):
        rospy.init_node(name, log_level=rospy.INFO)

        self.finger_num = finger_num
        self.base_angle = 360 // finger_num
        self.hand_radius_mm = hand_radius_mm  # length between center and edge
        self.finger_radius_mm = finger_radius_mm
        self.hand_mount_rotation = hand_mount_rotation
        self.approach_coef = approach_coef
        self.debug = debug
        cam_info: CameraInfo = rospy.wait_for_message(info_topic, CameraInfo, timeout=None)
        frame_size = (cam_info.height, cam_info.width)

        # convert unit
        # ref: https://qiita.com/srs/items/e3412e5b25477b46f3bd
        flatten_corrected_params = cam_info.P
        fp_x, fp_y = flatten_corrected_params[0], flatten_corrected_params[5]
        fp = (fp_x + fp_y) / 2

        # Publishers
        self.dbg_info_publisher = rospy.Publisher("/grasp_detection_server/result/debug", GraspDetectionDebugInfo, queue_size=10) if debug else None
        # Action Clients
        self.is_client = InstanceSegmentationClient()
        self.cdt_client = ComputeDepthThresholdClient() if enable_depth_filter else None
        self.tf_client = TFClient("base_link") # base_linkの座標系に変換するtf変換クライアント
        self.visualize_client = VisualizeClient()
        # Others
        self.bridge = CvBridge()
        self.projector = PointProjector(cam_info)
        self.pose_estimator = PoseEstimator()
        self.grasp_detector = GraspDetector(finger_num=finger_num, hand_radius_mm=hand_radius_mm,
                                            finger_radius_mm=finger_radius_mm,
                                            frame_size=frame_size, fp=fp)

        self.pool = Pool(100)

        self.server = SimpleActionServer(name, GraspDetectionAction, self.callback, False)
        self.server.start()

    def depth_filtering(self, img_msg, depth_msg, img, depth, masks, thresh=0.5, n=5):
        vis_base_img_msg = img_msg
        flont_indexes = []

        if self.cdt_client:
            merged_mask = np.where(np.sum(masks, axis=0) > 0, 255, 0).astype("uint8")
            # depthの欠損対策だがすでに不要
            # merged_mask = np.where(merged_mask * depth > 0, 255, 0).astype("uint8")
            whole_mask_msg = self.bridge.cv2_to_imgmsg(merged_mask)
            opt_depth_th = self.cdt_client.compute(depth_msg, whole_mask_msg, n=n)
            raw_flont_mask = extract_flont_mask_with_thresh(depth, opt_depth_th, n=n)
            flont_indexes = extract_flont_instance_indexes(raw_flont_mask, masks, thresh)
            flont_mask = merge_mask(np.array(masks)[flont_indexes])
            gray_3c = convert_rgb_to_3dgray(img)
            reversed_flont_mask = cv2.bitwise_not(flont_mask)
            base_img = cv2.bitwise_and(img, img, mask=flont_mask) + \
                cv2.bitwise_and(gray_3c, gray_3c, mask=reversed_flont_mask)
            vis_base_img_msg = self.bridge.cv2_to_imgmsg(base_img)
            rospy.loginfo(opt_depth_th)

        return vis_base_img_msg, flont_indexes

    def compute_object_center_pose_stampd(self, depth, mask, c_3d_c_on_surface, header):
        c_3d_c = Point(c_3d_c_on_surface.x, c_3d_c_on_surface.y, c_3d_c_on_surface.z)
        c_3d_w = self.tf_client.transform_point(header, c_3d_c)
        c_orientation = self.pose_estimator.get_orientation(depth, mask)

        return PoseStamped(
            Header(frame_id="base_link"),
            Pose(
                position=c_3d_w.point,
                orientation=c_orientation
            )
        )

    def compute_approach_distance(self, c_3d_c_on_surface, insertion_points_c):
        # bottom_z = min([pt.z for pt in insertion_points_c])
        bottom_z = max([pt.z for pt in insertion_points_c])
        top_z = c_3d_c_on_surface.z
        # length_to_center = bottom_z - top_z
        length_to_center = (bottom_z - top_z) * self.approach_coef  # インスタンス頂点からのアプローチ距離
        return length_to_center

    def compute_object_3d_radiuses(self, depth, bbox_handler):
        bbox_short_side_3d, bbox_long_side_3d = bbox_handler.get_sides_3d(self.projector, depth)
        short_raidus = bbox_short_side_3d / 2
        long_radius = bbox_long_side_3d / 2
        return (short_raidus, long_radius)

    def augment_angles(self, angle):
        angles = []
        for i in range(1, self.finger_num + 1):
            raw_rotated_angle = angle - (self.base_angle * i)
            rotated_angle = raw_rotated_angle + 360 if raw_rotated_angle < -360 else raw_rotated_angle
            reversed_rotated_angle = rotated_angle + 360
            angles.extend([rotated_angle, reversed_rotated_angle])
        angles.sort(key=abs)
        return angles

    def callback(self, goal: GraspDetectionGoal):
        print("receive request")
        img_msg = goal.image
        depth_msg = goal.depth
        points_msg = goal.points
        frame_id = img_msg.header.frame_id
        stamp = img_msg.header.stamp
        header = Header(frame_id=frame_id, stamp=stamp)
        try:
            start_time = time()
            img = self.bridge.imgmsg_to_cv2(img_msg)
            depth = self.bridge.imgmsg_to_cv2(depth_msg)
            instances = self.is_client.predict(img_msg) # List[Instance]
            # TODO: depthしきい値を求めるためにmerged_maskが必要だが非効率なので要改善
            masks = [self.bridge.imgmsg_to_cv2(instance_msg.mask) for instance_msg in instances]
            # TODO: compute n by camera distance
            vis_base_img_msg, flont_indexes = self.depth_filtering(img_msg, depth_msg, img, depth, masks, thresh=0.8, n=5)
            if not self.cdt_client:
                flont_indexes = list(range(len(instances)))
            flont_indexes_set = set(flont_indexes)

            objects: List[DetectedObject] = []  # 空のものは省く

            # 把持候補の生成, gpuプログラミングは並列処理できない
            results = []
            for i in range(len(instances)):
                # ignore other than instances are located on top of stacks
                # TODO: しきい値で切り出したマスク内に含まれないインスタンスはスキップ
                instance_msg = instances[i]
                mask = masks[i]
                if i not in flont_indexes_set:
                    continue
                results.append(self.grasp_detector.detect(img, depth, mask, instance_msg))

            # TODO: 座標変換も並列処理化したい
            for obj_index, candidate in enumerate(results):
                # 3d projection
                insertion_points_c = [self.projector.screen_to_camera_2(points_msg, uv) for uv in candidate.get_insertion_points_uv()]
                c_3d_c_on_surface = self.projector.screen_to_camera_2(points_msg, candidate.get_center_uv())
                # compute approach distance
                length_to_center = self.compute_approach_distance(c_3d_c_on_surface, insertion_points_c)
                center_pose_stamped_msg = self.compute_object_center_pose_stampd(depth, mask, c_3d_c_on_surface, header)
                angle = candidate.angle - self.hand_mount_rotation
                # 絶対値が最も小さい角度
                nearest_angle = self.augment_angles(angle)[0]
                objects.append(DetectedObject(
                    # points=insertion_points_msg,
                    center_pose=center_pose_stamped_msg, # yes
                    angle=nearest_angle, # yes
                    # short_radius=short_radius_3d,
                    # long_radius=long_radius_3d,
                    length_to_center=length_to_center, # yes
                    score=candidate.total_score, # yes
                    index=obj_index  # for visualize
                )
                )

            spent = time() - start_time
            print(f"stamp: {stamp.to_time()}, spent: {spent:.3f}, objects: {len(objects)} ({len(instances)})")
            self.server.set_succeeded(GraspDetectionResult(header, objects))

        except Exception as err:
            rospy.logerr(err)


if __name__ == "__main__":
    finger_num = rospy.get_param("finger_num")
    unit_angle = rospy.get_param("unit_angle")
    hand_radius_mm = rospy.get_param("hand_radius_mm")
    finger_radius_mm = rospy.get_param("finger_radius_mm")
    hand_mount_rotation = rospy.get_param("hand_mount_rotation")
    approach_coef = rospy.get_param("approach_coef")
    elements_th = rospy.get_param("elements_th")
    el_insertion_th = rospy.get_param("el_insertion_th")
    el_contact_th = rospy.get_param("el_contact_th")
    el_bw_depth_th = rospy.get_param("el_bw_depth_th")
    info_topic = rospy.get_param("image_info_topic")
    enable_depth_filter = rospy.get_param("enable_depth_filter")
    enable_candidate_filter = rospy.get_param("enable_candidate_filter")
    debug = rospy.get_param("debug")

    GraspDetectionServer(
        "detect_server",
        finger_num=finger_num,
        hand_radius_mm=hand_radius_mm,
        finger_radius_mm=finger_radius_mm,
        hand_mount_rotation=hand_mount_rotation,
        approach_coef=approach_coef,
        info_topic=info_topic,
        enable_depth_filter=enable_depth_filter,
        debug=debug
    )
    rospy.spin()
