#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/register_node_macro.hpp>
namespace devdash_test {
class Probe : public rclcpp::Node {
public:
  explicit Probe(const rclcpp::NodeOptions &options)
      : rclcpp::Node("devdash_probe", options) {}
};
}
RCLCPP_COMPONENTS_REGISTER_NODE(devdash_test::Probe)
