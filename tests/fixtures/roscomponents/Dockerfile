FROM ros:jazzy-ros-base
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake ros-jazzy-launch-ros ros-jazzy-rclcpp-components \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
