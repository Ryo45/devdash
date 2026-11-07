# devdash-roslaunch

ROS 2 JazzyのLaunchServiceをそのまま実行し、Actionとプロセスの対応をイベントから取得する独立したBridgeです。
PIDへの推測・直接kill、launchファイルの独自解析、独自のrespawn、Actionのprivate状態の書き換えは行いません。
通常のExecuteProcess／Nodeは監視・停止を提供します。個別Start／Restartは非対応です。

## 導入と実行

ROS 2 Jazzyの環境をsourceして使用してください。PyPIにある同名の`launch`をインストールする方式ではありません。
ROS環境の`launch`、`launch_ros`を使用し、コンポーネント連携には`rclpy`、`composition_interfaces`も必要です。

```bash
source /opt/ros/jazzy/setup.bash
python3 -m venv --system-site-packages /tmp/devdash-roslaunch-env
/tmp/devdash-roslaunch-env/bin/pip install ./bridges/roslaunch
/tmp/devdash-roslaunch-env/bin/devdash-roslaunch \
  --socket /run/user/1000/devdash/robot-launch.sock \
  /path/to/robot.launch.py use_sim_time:=false
```

既存のPython/XML/YAML launchはROS自身のAnyLaunchDescriptionSourceへ渡します。
配下のプロセスをsystemdで管理する場合は、unitのExecStartでこのラッパーを起動します。
Dockerの場合もコンテナの起動定義側でラッパーを使い、devdash設定のcontroller.kindをroslaunchにします。

## 操作能力

| 対象 | 監視 | 個別操作 |
| --- | --- | --- |
| 通常のExecuteProcess / Node | 起動、PID、終了コード、respawnを追跡 | ShutdownProcessによるStop。Start／Restartは拒否 |
| 通常のComposableNodeContainerの配下 | ComponentManagerのListNodesから取得 | Unloadのみ。元のLoadNode要求が分からないためLoad／Reloadは拒否 |
| ManagedComponentsで明示したComposableNode | 同じ論理IDでロード／アンロード状態を保持 | Load／Unload／Reload。最初に評価したLoadNode要求を再利用 |

通常のプロセス停止には元のon_exit等のイベントハンドラが実行され、他のプロセスやlaunch全体へ影響する場合があります。
停止手順・エスカレーション・respawnの抑止はlaunch自身に委譲します。Bridgeが復旧操作を追加することはありません。

同じExecuteLocal Actionの再実行はJazzyで拒否されます。新しいActionを作れば別PIDを起動できますが、
元のActionを対象にしたイベントハンドラは引き継がれません。そのため自動複製やlaunch全体の再評価によるRestartは実装していません。

## 再ロード可能なComposable Nodeの宣言

元のComposableNode定義を一箇所だけ保持し、明示的に連携する場合の例です。

```python
from launch import LaunchDescription
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode
from devdash_roslaunch.actions import ManagedComponents

def generate_launch_description():
    container = ComposableNodeContainer(
        name='components', namespace='/robot',
        package='rclcpp_components', executable='component_container',
        composable_node_descriptions=[], output='screen')
    managed = ManagedComponents(container=container, nodes={
        'camera': ComposableNode(
            package='my_camera', plugin='my_camera::Camera',
            name='camera', namespace='/robot', parameters=[{'use_sim_time': False}]),
    })
    return LaunchDescription([container, managed])
```

Jazzyの`get_composable_node_load_request`で、元のlaunch contextのnamespace・parameters・remaps等を評価します。
ROSメッセージとして保持した要求をComponentManagerへ再送します。ReloadはUnload成功後にLoadし、失敗時のロールバックはしません。
Lifecycle／独自ComposableNodeサブクラスは対応対象外です。外部のComponentManagerや普通のNodeとして起動したmanagerは現在対象外です。
対応するのは、同じLaunchServiceが把握しているComposableNodeContainer Actionです。

外部からアンロードされた場合は状態を更新するだけで、自動ロードはしません。
managerのrespawn時も自動再ロードせず、接続世代を変えて新しい明示操作を待ちます。
不明なLoad結果をノード名で推測して自動採用することもありません。

## IPCと通知

ローカルUnix Socket（0600）とJSON一行を使用します。外部HTTP APIは公開しません。

- `{"command":"snapshot"}`: epoch、sequence、childrenを返す。
- `{"command":"watch"}`: 初期Snapshotと変更時のSnapshotを継続通知する。
- `{"command":"operate","protocol":1,"id":"...","epoch":"...","action":"stop"}`: 能力を確認して操作する。

応答は`{"ok":true,"data":...,"error":null}`です。古いepochの操作は拒否します。
プロセスのepochはBridge UUID、コンポーネントのepochはBridge UUID＋manager Actionの起動世代です。
devdash Coreの初期接続はSnapshotポーリングです。watchは独立クライアント向けに実装・検証しており、Coreへの常時ストリーム接続は今後の拡張です。

ROSサービスにはmanager世代を条件にした原子的なUnloadがありません。前後で世代を検査しますが、
同時のmanager再起動とサービス要求の競合を完全には排除できません。タイムアウト後も要求が適用される可能性があり、自動再試行は行いません。
コンポーネント単位にCPU・メモリや専用PIDを割り当てず、共有プロセスの子要素として表示します。

## 検証

`tools/ros_launch_probe.py`と`tests/roslaunch/test_bridge.py`を実ROS 2 Jazzy上で検証しています。
再実行方法と検証結果は[設計・検証記録](../../docs/container-processes-and-roslaunch.md)を参照してください。

## 独立した版とIPC互換性

Bridgeは`devdash-roslaunch 0.1.1`としてCore/GUIとは別に配布します。
`devdash-roslaunch --version`またはdistributionメタデータから版を確認できます。起動せずに確認でき、ROSノードは開始しません。
Snapshotには`bridge: {version: ..., protocol: 1}`を含めます。パッケージ版が違ってもIPC世代が合えば利用できます。

`operate`には`protocol: 1`が必要です。互換性確認のSnapshotと操作は同じ接続で送れます。
既存のepoch検査も維持し、古いBridgeやComponentManager世代を操作しません。Snapshot/watchは互換性調査のため取得できます。
Core 0.2.0は無世代の旧Bridgeへ子操作を送らず、版・非互換理由を表示します。0.1.0からは一度更新が必要です。
以後、Core更新ごとにBridgeも更新する必要はありません。

配置・手動更新・ロールバックは[常駐運用](../../docs/resident-operation.md)を参照してください。
Core用venvにROSを導入せず、BridgeはROSのvenvまたはイメージ側に配置します。
