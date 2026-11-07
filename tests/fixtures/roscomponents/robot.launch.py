from launch import LaunchDescription
from launch.actions import ExecuteProcess
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode
from devdash_roslaunch.actions import ManagedComponents

def generate_launch_description():
    container = ComposableNodeContainer(
        name='devdash_component_manager', namespace='/devdash_test',
        package='rclcpp_components', executable='component_container',
        composable_node_descriptions=[ComposableNode(
            package='devdash_test_components', plugin='devdash_test::Probe', name='native_probe')],
        output='screen')
    return LaunchDescription([
        ExecuteProcess(cmd=['/usr/bin/sleep', '60'], respawn=True, respawn_delay=.1),
        container,
        ManagedComponents(container=container, nodes={
            'probe': ComposableNode(package='devdash_test_components', plugin='devdash_test::Probe',
                                    name='managed_probe', namespace='/devdash_test',
                                    parameters=[{'use_sim_time': False}]),
        }),
    ])
