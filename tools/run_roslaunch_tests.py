#!/usr/bin/env python3
"""Run in an isolated ROS 2 Jazzy environment (recommended: Docker --network none).
Requires CMake, C++, launch_ros, rclcpp_components, rclpy. No host units are used.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
def run(command,env):
    subprocess.run(command,env=env,check=True)
def main():
    if os.environ.get('ROS_DISTRO')!='jazzy':
        raise SystemExit('Source ROS 2 Jazzy setup.bash before running these optional tests')
    with tempfile.TemporaryDirectory(prefix='devdash-ros-tests-') as temporary:
        directory=Path(temporary);install=directory/'install'
        env=dict(os.environ,ROS_LOG_DIR=str(directory/'logs'),ROS_DOMAIN_ID='191',ROS_AUTOMATIC_DISCOVERY_RANGE='LOCALHOST')
        env.pop('ROS_LOCALHOST_ONLY',None)
        run([sys.executable,str(ROOT/'tools/ros_launch_probe.py')],env)
        run(['cmake','-S',str(ROOT/'tests/fixtures/roscomponents'),'-B',str(directory/'build'),'-DCMAKE_INSTALL_PREFIX='+str(install)],env)
        run(['cmake','--build',str(directory/'build'),'-j','1'],env)
        run(['cmake','--install',str(directory/'build')],env)
        env['AMENT_PREFIX_PATH']=str(install)+os.pathsep+env.get('AMENT_PREFIX_PATH','')
        env['LD_LIBRARY_PATH']=str(install/'lib')+os.pathsep+env.get('LD_LIBRARY_PATH','')
        run([sys.executable,str(ROOT/'tests/roslaunch/test_bridge.py')],env)
        print(json.dumps({'ros_distro':'jazzy','action_restart_probe':'passed','bridge_and_components':'passed','domain_id':191}))
if __name__=='__main__':main()
