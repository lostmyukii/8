try:
    import rclpy
    from rclpy.node import Node
except ImportError:
    rclpy = None
    Node = object
from typing import Dict, List, Optional, Set, Tuple

# 导入标准 ROS 2 消息类型
# 对应 micro-ROS 发出的 std_msgs/msg/UInt16MultiArray
from std_msgs.msg import UInt16MultiArray
# 对应 micro-ROS 接收的 std_msgs/msg/Int32
from std_msgs.msg import Int32

Coord = Tuple[int, int]


class MazeExplorer:
    def __init__(self, blocked_threshold: int = 290, max_steps: int = 2000) -> None:
        self.blocked_threshold = blocked_threshold
        self.max_steps = max_steps

        self.pos: Coord = (0, 0)
        self.start: Coord = (0, 0)
        self.heading = 0
        self.visited: Set[Coord] = {self.pos}
        self.stack: List[Coord] = []
        self.cells: Dict[Coord, Dict[str, Optional[bool]]] = {}
        self.pending_cmds: List[int] = []
        self.steps = 0
        self.done = False

        self.min_x = 0
        self.max_x = 0
        self.min_y = 0
        self.max_y = 0

    def observe(self, front: int, back: int, right: int, left: int) -> None:
        if self.done:
            return
        local = {"front": front, "back": back, "right": right, "left": left}
        for local_dir, dist in local.items():
            gdir = self._local_to_global_dir(local_dir)
            self._set_wall(self.pos, gdir, dist < self.blocked_threshold)

    def next_move_command(self) -> Optional[int]:
        if self.done:
            return None
        if self.steps >= self.max_steps:
            self.done = True
            return None

        if self.pending_cmds:
            cmd = self.pending_cmds.pop(0)
            self._apply_cmd(cmd)
            return cmd

        cell = self._cell(self.pos)
        for direction in ("N", "E", "W", "S"):
            blocked = cell.get(direction)
            if blocked is not False:
                continue
            npos = self._neighbor(self.pos, direction)
            if npos in self.visited:
                continue
            self.stack.append(self.pos)
            self.visited.add(npos)
            self.pending_cmds.extend(self._plan_step_to_global_dir(direction))
            cmd = self.pending_cmds.pop(0)
            self._apply_cmd(cmd)
            return cmd

        if self.stack:
            target = self.stack.pop()
            direction = self._delta_to_dir(target[0] - self.pos[0], target[1] - self.pos[1])
            if direction is None:
                self.done = True
                return None
            self.pending_cmds.extend(self._plan_step_to_global_dir(direction))
            cmd = self.pending_cmds.pop(0)
            self._apply_cmd(cmd)
            return cmd

        self.done = True
        return None

    def render_ascii(self) -> str:
        if not self.cells:
            self._cell(self.pos)

        min_x = self.min_x
        max_x = self.max_x
        min_y = self.min_y
        max_y = self.max_y

        lines: List[str] = []
        for y in range(max_y, min_y - 1, -1):
            top = "+"
            for x in range(min_x, max_x + 1):
                w = self._cell((x, y)).get("N")
                top += self._hseg(w) + "+"
            lines.append(top)

            mid = ""
            for x in range(min_x, max_x + 1):
                cell = self._cell((x, y))
                left_wall = self._vseg(cell.get("W"))
                c = " "
                if (x, y) == self.pos:
                    c = {0: "^", 1: ">", 2: "v", 3: "<"}[self.heading]
                elif (x, y) == self.start:
                    c = "S"
                elif (x, y) in self.visited:
                    c = "."
                mid += f"{left_wall} {c} "
            right_wall = self._vseg(self._cell((max_x, y)).get("E"))
            mid += right_wall
            lines.append(mid)

        bottom = "+"
        for x in range(min_x, max_x + 1):
            w = self._cell((x, min_y)).get("S")
            bottom += self._hseg(w) + "+"
        lines.append(bottom)

        return "\n".join(lines)

    def _cell(self, pos: Coord) -> Dict[str, Optional[bool]]:
        if pos not in self.cells:
            self.cells[pos] = {"N": None, "S": None, "E": None, "W": None}
        return self.cells[pos]

    def _set_wall(self, pos: Coord, direction: str, blocked: bool) -> None:
        cell = self._cell(pos)
        cell[direction] = blocked

        npos = self._neighbor(pos, direction)
        opp = {"N": "S", "S": "N", "E": "W", "W": "E"}[direction]
        ncell = self._cell(npos)
        ncell[opp] = blocked

    def _neighbor(self, pos: Coord, direction: str) -> Coord:
        dx, dy = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}[direction]
        return (pos[0] + dx, pos[1] + dy)

    def _move_to(self, pos: Coord) -> None:
        self.pos = pos
        self.min_x = min(self.min_x, pos[0])
        self.max_x = max(self.max_x, pos[0])
        self.min_y = min(self.min_y, pos[1])
        self.max_y = max(self.max_y, pos[1])

    def _apply_cmd(self, cmd: int) -> None:
        if cmd == 3:
            self.heading = (self.heading + 1) % 4
            return
        if cmd == 4:
            self.heading = (self.heading + 3) % 4
            return
        if cmd == 1:
            self._move_to(self._neighbor(self.pos, self._heading_to_global_dir(self.heading)))
            self.steps += 1
            return
        if cmd == 2:
            self._move_to(self._neighbor(self.pos, self._heading_to_global_dir((self.heading + 2) % 4)))
            self.steps += 1
            return
        self.done = True

    def _heading_to_global_dir(self, heading: int) -> str:
        return {0: "N", 1: "E", 2: "S", 3: "W"}[heading]

    def _global_dir_to_heading(self, direction: str) -> int:
        return {"N": 0, "E": 1, "S": 2, "W": 3}[direction]

    def _local_to_global_dir(self, local_dir: str) -> str:
        order = ["N", "E", "S", "W"]
        idx = {"front": 0, "right": 1, "back": 2, "left": 3}[local_dir]
        return order[(self.heading + idx) % 4]

    def _plan_step_to_global_dir(self, direction: str) -> List[int]:
        desired_heading = self._global_dir_to_heading(direction)
        delta = (desired_heading - self.heading) % 4
        if delta == 0:
            return [1]
        if delta == 2:
            return [2]
        if delta == 1:
            return [3, 1]
        return [4, 1]

    def _delta_to_dir(self, dx: int, dy: int) -> Optional[str]:
        if (dx, dy) == (0, 1):
            return "N"
        if (dx, dy) == (0, -1):
            return "S"
        if (dx, dy) == (1, 0):
            return "E"
        if (dx, dy) == (-1, 0):
            return "W"
        return None

    def _hseg(self, wall: Optional[bool]) -> str:
        if wall is True:
            return "---"
        if wall is False:
            return "   "
        return " ? "

    def _vseg(self, wall: Optional[bool]) -> str:
        if wall is True:
            return "|"
        if wall is False:
            return " "
        return "?"


class PCInteractionNode(Node):
    def __init__(self):
        # 初始化节点名称，可以是任意合法的 ROS 2 节点名
        super().__init__('pc_interaction_node')
        
        # ---------------------------------------------------------
        # 1. 订阅部分：接收 micro-ROS 发来的数组数据
        # ---------------------------------------------------------
        # 话题名称必须与 micro-ROS 代码中的发布话题完全一致
        # micro-ROS 代码中定义的发布者话题: "micro_ros_uint16_array_publisher"
        self.subscription_array = self.create_subscription(
            UInt16MultiArray,
            'micro_ros_uint16_array_publisher',
            self.array_callback,
            10  # QoS 队列深度
        )
        # 防止订阅对象被意外垃圾回收
        self.subscription_array

        # ---------------------------------------------------------
        # 2. 发布部分：向 micro-ROS 发送控制指令
        # ---------------------------------------------------------
        # 话题名称必须与 micro-ROS 代码中的订阅话题完全一致
        # micro-ROS 代码中定义的订阅者话题: "micro_ros_int32_subscriber"
        self.publisher_led = self.create_publisher(
            Int32,
            'micro_ros_int32_subscriber',
            10
        )
        self.explorer = MazeExplorer(blocked_threshold=200, max_steps=2000)
        self.last_command_ns = 0
        self.command_interval_ns = int(0.25 * 1e9)
        self.render_every_steps = 0
        self.final_map_printed = False

        self.get_logger().info('PC Interaction Node Started')
        self.get_logger().info('Listening to: /micro_ros_uint16_array_publisher')
        self.get_logger().info('Publishing to: /micro_ros_int32_subscriber')

    def array_callback(self, msg):
        received_data = list(msg.data)
        if len(received_data) < 4:
            return

        front = int(received_data[0])
        back = int(received_data[1])
        right = int(received_data[2])
        left = int(received_data[3])

        self.explorer.observe(front=front, back=back, right=right, left=left)

        now_ns = int(self.get_clock().now().nanoseconds)
        if now_ns - self.last_command_ns < self.command_interval_ns:
            return

        cmd = self.explorer.next_move_command()
        if cmd is None or cmd == 0:
            if self.explorer.done:
                self.get_logger().info("Maze exploration finished.")
                if (not self.final_map_printed) and (self.explorer.pos == self.explorer.start):
                    self.get_logger().info("\n" + self.explorer.render_ascii())
                    self.final_map_printed = True
            return

        out = Int32()
        out.data = int(cmd)
        self.publisher_led.publish(out)
        self.last_command_ns = now_ns

        if self.render_every_steps > 0 and self.explorer.steps % self.render_every_steps == 0:
            self.get_logger().info("\n" + self.explorer.render_ascii())

def main(args=None):
    # 初始化 ROS 2 Python 客户端库
    rclpy.init(args=args)

    # 创建节点实例
    node = PCInteractionNode()

    try:
        # 保持节点运行，等待回调
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # 销毁节点并关闭库
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
