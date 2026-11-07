# Python版のコンテナ・ROS 2 launch連携

Docker EngineのローカルUnix Socketを使用し、既存コンテナ名またはCompose project/serviceから現在のIDを解決します。
複数レプリカに曖昧一致した場合は拒否します。コンテナ作成・Compose upは行いません。
`examples/docker-supervisor.yaml`に設定例があります。

OSプロセスはDocker topで一覧取得し、PIDはDocker daemonのホスト名前空間として明示します。
CPUはpsの稼働期間平均、メモリはRSSです。OSプロセスにStart/Stop/Restart能力を付けません。
Supervisorの論理サービスは別要素として扱い、停止中でも表示します。異なるPID名前空間を推測で結びません。

supervisordの操作は、Docker execで標準ライブラリだけの一回限りのPython RPCクライアントを実行し、
既存のUnix Socket/XML-RPCへ委譲します。常駐Agentは入れません。
コンテナID＋起動時刻＋Supervisor世代をUIから返してもらい、変わっていれば操作を拒否します。
接続エラーや結果不明時に操作を再送しません。停止・起動のポリシーは既存Supervisorが正本です。
Docker socketへのアクセス権は既存の権限設定に従います。

ReadyはDocker Healthcheckを優先し、未設定ならRunningを使います。子プロセス停止からコンテナHealthを再定義しません。
s6などの追加Supervisorは未対応です。

ROS 2は[既存の独立Python Bridge](../bridges/roslaunch/README.md)を再利用しています。
通常のlaunch Actionは監視・停止、ManagedComponentsで明示したコンポーネントはload/unload/reloadが対象です。
起動済みActionの再利用がJazzyで拒否され、再生成も元のイベントハンドラを保持できないため、通常Actionの個別Start/Restartを提供しません。
対応能力はGUIのボタンとDetailsに反映します。launchの独自解析、ノード名からPID推定、独自respawnは行いません。

任意のJazzyテストはROSのlaunch、launch_ros、rclpy、rclcpp_components、CMake/C++のある環境で実行します。
C++は実コンポーネントを使うテストfixtureだけに必要で、Pythonアプリのビルドには不要です。

```bash
source /opt/ros/jazzy/setup.bash
python3 tools/run_roslaunch_tests.py
```

このブランチで新しく実行したDocker・GUI・Coreの検証範囲は[Python版検証記録](python-alternative.md)を参照してください。
