#!/usr/bin/env python

from __future__ import absolute_import, division, print_function

import numpy as np
import rospy
from .gym_gazebo_env import GymGazeboEnv
from gym.envs.registration import register
from std_msgs.msg import Float64
from gazebo_msgs.msg import LinkStates, ModelStates, ModelState, LinkState
from std_msgs.msg import Float32MultiArray
from geometry_msgs.msg import Pose, Twist
from sensor_msgs.msg import Image
from tf.transformations import quaternion_from_euler
import cv2
from cv_bridge import CvBridge, CvBridgeError
import math


"""
CameraSensor
"""
class CameraSensor():
    def __init__(self, resolution=(64,64), noise=0.0):
        self.resolution = resolution
        self.noise = noise
        self.bridge = CvBridge()
        self.image_sub = rospy.Subscriber('/front_cam/image_raw', Image, self._image_cb)
        self.rgb_image = None

    def _image_cb(self,data):
        try:
            self.rgb_image = self.bridge.imgmsg_to_cv2(data, "bgr8")
            # show image
            cv2.namedWindow("door opening")
            cv2.imshow('door opening',self.rgb_image)
            cv2.waitKey(1)
        except CvBridgeError as e:
            print(e)

    def check_camera_ready(self):
        self.rgb_image = None
        rospy.logdebug("Waiting for /front_cam/image_raw to be READY...")
        while self.rgb_image is None and not rospy.is_shutdown():
            try:
                data = rospy.wait_for_message("/front_cam/image_raw", Image, timeout=5.0)
                self.rgb_image = self.bridge.imgmsg_to_cv2(data, "bgr8")
                rospy.logdebug("Current /front_cam/image_raw READY=>")
            except:
                rospy.logerr("Current /front_cam/image_raw not ready yet, retrying for getting /front_cam/image_raw")

    def image_arr(self):
        img = cv2.resize(self.rgb_image, self.resolution)
        img_arr = np.array(img)/255 - 0.5 # normalize the image for easier training
        img_arr = img_arr.reshape((64,64,3))
        # add noise
        return img_arr

"""
pose sensor
"""
class PoseSensor():
    def __init__(self, noise=0.0):
        self.noise = noise
        self.door_pose_sub = rospy.Subscriber('/gazebo/link_states', LinkStates, self._door_pose_cb)
        self.robot_pos_sub = rospy.Subscriber('/gazebo/model_states', ModelStates, self._robot_pose_cb)
        self.robot_pose = None
        self.door_pose = None

    def _door_pose_cb(self,data):
        index = data.name.index('hinged_door::door')
        self.door_pose = data.pose[index]

    def _robot_pose_cb(self,data):
        index = data.name.index('mobile_robot')
        self.robot_pose = data.pose[index]

    def robot(self):
        # add noise
        return self.robot_pose

    def door(self):
        return self.door_pose

    def check_sensor_ready(self):
        self.robot_pose = None
        rospy.logdebug("Waiting for /gazebo/model_states to be READY...")
        while self.robot_pose is None and not rospy.is_shutdown():
            try:
                data = rospy.wait_for_message("/gazebo/model_states", ModelStates, timeout=5.0)
                index = data.name.index('mobile_robot')
                self.robot_pose = data.pose[index]
                rospy.logdebug("Current  /gazebo/model_states READY=>")
            except:
                rospy.logerr("Current  /gazebo/model_states not ready yet, retrying for getting  /gazebo/model_states")

        self.door_pose = None
        rospy.logdebug("Waiting for /gazebo/link_states to be READY...")
        while self.door_pose is None and not rospy.is_shutdown():
            try:
                data = rospy.wait_for_message("/gazebo/link_states", LinkStates, timeout=5.0)
                index = data.name.index('hinged_door::door')
                self.door_pose = data.pose[index]
                rospy.logdebug("Current  /gazebo/link_states READY=>")
            except:
                rospy.logerr("Current  /gazebo/link_states not ready yet, retrying for getting  /gazebo/link_states")

"""
Robot Driver
"""
class RobotDriver():
    def __init__(self, noise=0.0):
        self.noise = noise
        self.vel_pub = rospy.Publisher('cmd_vel', Twist, queue_size=1)

    def drive(self,vx,vyaw):
        msg = Twist()
        msg.linear.x = vx
        msg.linear.y = 0
        msg.linear.z = 0
        msg.angular.x = 0
        msg.angular.y = 0
        msg.angular.z = vyaw
        self.vel_pub.publish(msg)
        # add noise

    def stop(self):
        self.drive(0,0)

    def check_connection(self):
      rate = rospy.Rate(10)  # 10hz
      while self.vel_pub.get_num_connections() == 0 and not rospy.is_shutdown():
        rospy.logdebug("No susbribers to vel_pub yet so we wait and try again")
        try:
          rate.sleep()
        except rospy.ROSInterruptException:
          # This is to avoid error when world is rested, time when backwards.
          pass
      rospy.logdebug("vel_pub Publisher Connected")

###############################################################################
register(
  id='DoorOpenTash-v0',
  entry_point='envs.door_open_task_env:DoorOpenTaskEnv')

class DoorOpenTaskEnv(GymGazeboEnv):

  def __init__(self,resolution=(64,64),noise=0.0):
    """
    Initializes a new DoorOpenTaskEnv environment.
    """
    super(DoorOpenTaskEnv, self).__init__(
      start_init_physics_parameters=False,
      reset_world_or_sim="WORLD"
    )

    self.door_dim = [0.9144, 0.0698] # length, width
    self.info = {}
    self.max_episode_steps = 100
    self.action_space = 2*np.array([[1.5,3.14],[1.5,0.0],[0.0,3.14],[-1.5,3.14],[-1.5,0.0]]) # x and yaw velocities
    self.step_cnt = 0
    self.door_angle = 0.1 # inital angle of door
    self.open = False

    rospy.logdebug("Start DoorOpenTaskEnv INIT...")
    self.gazebo.unpauseSim()

    self.camera = CameraSensor(resolution,noise)
    self.driver = RobotDriver(noise)
    self.pose_sensor = PoseSensor(noise)
    
    self._check_all_sensors_ready()
    self.robot_pose_pub = rospy.Publisher('/gazebo/set_model_state', ModelState, queue_size=1)
    self._check_publisher_connection()

    self.gazebo.pauseSim()
    rospy.logdebug("Finished DoorOpenTaskEnv INIT...")

  def _set_init(self):
    """Sets the Robot in its init pose
    """
    # reset mobile rebot velocity as stop
    self.driver.stop()
    # reset mobile_robot position
    self._reset_mobile_robot(1.5,0.5,0.075,3.14)
    # wait the door closed
    self._wait_door_closed()
    # reset mobile robot position to open door
    self._reset_mobile_robot(0.61,0.77,0.075,3.3)

    self.step_cnt = 0
    self.open_angle = 0.1
    self.open = False

  def _check_all_systems_ready(self):
    """
    Checks that all the sensors, publishers and other simulation systems are
    operational.
    """
    self._check_all_sensors_ready()
    self._check_publisher_connection()

  def _check_all_sensors_ready(self):
    self.camera.check_camera_ready()
    self.pose_sensor.check_sensor_ready()
    rospy.logdebug("All Sensors READY")

  def _check_publisher_connection(self):
    self.driver.check_connection()
    rospy.logdebug("All Publishers READY")

  def _get_observation(self):
    obs = self.camera.image_arr()
    return obs

  def _post_information(self):
    return self.pose_sensor.robot()

  def _take_action(self, action_idx):
    _,self.door_angle = self._door_position()
    action = self.action_space[action_idx]
    self.driver.drive(action[0],action[1])
    rospy.sleep(0.5)
    self.step_cnt += 1
    self.open = self._door_is_open()

  def _is_done(self):
    if self._door_open_failed() or self._door_is_open() or self._robot_is_out():
        return True
    else:
        return False

  def _compute_reward(self):
    done_reward = 0
    if self.open:
        done_reward = 100
    else:
        door_r, door_a = self._door_position()
        delta_a = door_a-self.door_angle
        done_reward = delta_a*10

    # # failure penalty
    # failed_penalty = 0
    # if self._door_open_failed():
    #     failed_penalty = 10*(self.max_episode_steps-self.step_cnt)/self.max_episode_steps
    # # try to achieve less steps
    # step_penalty = 0.1
    #print("step reward and penalty: ", done_reward, failed_penalty, step_penalty)
    return done_reward


  #############################################################################
  # utility functions

  def _reset_mobile_robot(self,x,y,z,yaw):
      robot = ModelState()
      robot.model_name = 'mobile_robot'
      robot.pose.position.x = x
      robot.pose.position.y = y
      robot.pose.position.z = z
      rq = quaternion_from_euler(0,0,yaw)
      robot.pose.orientation.x = rq[0]
      robot.pose.orientation.y = rq[1]
      robot.pose.orientation.z = rq[2]
      robot.pose.orientation.w = rq[3]
      self.robot_pose_pub.publish(robot)

  def _wait_door_closed(self):
      door_r, door_a = self._door_position()
      while door_a > 0.11:
          rospy.sleep(1)
          door_r, door_a = self._door_position()

  def _door_is_open(self):
      door_r, door_a = self._door_position()
      if door_a > 0.45*math.pi: # 81 degree
          return True
      else:
          return False

  # check the position of camera
  # if it is in the door block, still trying
  # else failed, reset env
  def _door_open_failed(self):
      if not self._robot_is_out():
          campose_r, campose_a = self._camera_position()
          doorpose_r, doorpos_a = self._door_position()
          if campose_r > 1.1*doorpose_r or campose_a > 1.1*doorpos_a:
              return True
      return False

  # camera position in door polar coordinate frame
  # return radius to (0,0) and angle 0 for (0,1,0)
  def _camera_position(self):
      cam_pose = self._robot_footprint_position(0.5,-0.25)
      angle = math.atan2(cam_pose[0,3],cam_pose[1,3])
      radius = math.sqrt(cam_pose[0,3]*cam_pose[0,3]+cam_pose[1,3]*cam_pose[1,3])
      return radius, angle

  # door position in polar coordinate frame
  # retuen radius to (0,0) and angle 0 for (0,1,0)
  def _door_position(self):
      door_matrix = self._pose_matrix(self.pose_sensor.door())
      door_edge = np.array([[1,0,0,self.door_dim[0]],
                            [0,1,0,0],
                            [0,0,1,0],
                            [0,0,0,1]])
      door_edge_mat = np.dot(door_matrix, door_edge)
      # open angle [0, pi/2]
      open_angle = math.atan2(door_edge_mat[0,3],door_edge_mat[1,3])
      return self.door_dim[0], open_angle


  # robot is out of the door way (x < 0)
  def _robot_is_out(self):
      # footprint of robot
      footprint_lf = self._robot_footprint_position(0.25,0.25)
      footprint_lr = self._robot_footprint_position(-0.25,0.25)
      footprint_rf = self._robot_footprint_position(0.25,-0.25)
      footprint_rr = self._robot_footprint_position(-0.25,-0.25)
      if footprint_lf[0,3] < 0.0 and footprint_lr[0,3] < 0.0 and footprint_rf[0,3] < 0.0 and footprint_rr[0,3] < 0.0:
          return True
      else:
          return False

  # utility function
  def _robot_footprint_position(self,x,y):
      robot_matrix = self._pose_matrix(self.pose_sensor.robot())
      footprint_trans = np.array([[1,0,0,x],
                               [0,1,0,y],
                               [0,0,1,0],
                               [0,0,0,1]])
      fp_mat = np.dot(robot_matrix, footprint_trans)
      return fp_mat

  def _pose_matrix(self, cp):
    position = cp.position
    orientation = cp.orientation
    matrix = np.eye(4)
    # translation
    matrix[0,3] = position.x# in meter
    matrix[1,3] = position.y
    matrix[2,3] = position.z
    # quaternion to matrix
    x = orientation.x
    y = orientation.y
    z = orientation.z
    w = orientation.w

    Nq = w*w + x*x + y*y + z*z
    if Nq < 0.001:
        return matrix

    s = 2.0/Nq
    X = x*s
    Y = y*s
    Z = z*s
    wX = w*X; wY = w*Y; wZ = w*Z
    xX = x*X; xY = x*Y; xZ = x*Z
    yY = y*Y; yZ = y*Z; zZ = z*Z
    matrix=np.array([[1.0-(yY+zZ), xY-wZ, xZ+wY, position.x],
            [xY+wZ, 1.0-(xX+zZ), yZ-wX, position.y],
            [xZ-wY, yZ+wX, 1.0-(xX+yY), position.z],
            [0, 0, 0, 1]])
    return matrix
