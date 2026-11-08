# Lifecycle・Observer・Gateの分離

> 比較元Coordinator方式（main `d143e97`）の記録です。このStageブランチの現行仕様・検証は[比較レポート](stage-comparison.md)と[設定手順](stage-configuration.md)を参照してください。旧操作の説明をStage版へ適用しないでください。

mainの変更です。小さな変更ごとのリリースタグは追加せず、既存の設定とIPC major 1を維持します。

## 責務と設定

| 対象 | Lifecycle（親WorkloadのStart/Stop/Restart） | 内部観測 | 子の操作 |
| --- | --- | --- | --- |
| 通常のsystemd service | systemd user/system manager | systemd | なし |
| systemd管理コンテナ | systemd user/system manager | `observer.docker`のDocker API | 任意の`controller`（コンテナ内） |
| systemd管理ROS 2 launch | systemd user/system manager | systemd + `controller`のBridge snapshot | Bridgeが公開する対応操作のみ |
| Docker直接管理（従来設定） | Docker Engine | `docker`のDocker API | 任意の`controller`（コンテナ内） |

`backend`は引き続きLifecycle操作先です。`controller`は従来どおり内部Supervisor/Bridgeの設定であり、親のLifecycleを指しません。ホストBridgeは従来の設定だけで観測と子操作が可能です。汎用Adapter登録機構は増やしていません。

systemd Workloadに、次のようにDocker Observerを追加できます。詳細は[サンプル設定](../examples/systemd-docker.yaml)を参照してください。

```yaml
workloads:
  robot:
    backend: systemd
    scope: system
    unit: robot-container.service
    observer:
      docker:
        container: robot-dev
    controller: # 内部操作が必要な場合だけ。省略時はOSプロセスの観測のみ。
      kind: supervisord
      socket: /run/supervisor.sock
```

名前またはCompose project/serviceから毎回現在のコンテナを解決します。作成・自動登録しません。systemdとコンテナの対応は利用者が設定します。unitのExecStartから推測しません。

同じDocker selectorを、別のDocker直接管理Workloadとsystemd WorkloadのObserverの両方に指定する設定は拒否します。名前とCompose selectorが実際には同じコンテナを指すケースまでは静的に検出できません。

## 観測する事実を分ける

- `state` / PID / リソース / manager generationはLifecycle対象の実際の観測値です。systemd管理ならsystemd由来です。
- `internal`はDockerコンテナのstate・実体ID・起動時刻・PID・リソースなどです。systemdの観測値を上書きしません。
- `health`はDocker Healthcheckの報告です。Healthcheck未設定は「not reported」で、healthyとは表示しません。
- `readiness`は`state`（ready / not_ready / unknown）、`source`、`detail`です。Docker Healthcheckありならその結果、なしなら明示的にRunningのみの代替判定です。
- systemd `Type=notify` / `notify-reload`はmanagerがactiveになった起動完了をReadyとして表示します。継続的なHealth保証ではありません。`Type=simple` / `exec`等のactiveから機能的Readyを推測しません。
- `children`は観測した内部要素、`controller`は取得できたBridgeの互換性情報です。生PIDに操作ボタンを付けず、既存Controllerが公開した操作だけを使用します。
- `error`はLifecycle観測、`observer_error`は内部観測、`children_error`は内部詳細取得の問題です。Docker Health取得に成功していても、Supervisorだけ接続不能の場合があります。

systemdと内部Observerは並列に取得し、それぞれ3秒を上限にします。Docker切断でsystemdのactiveをunknown/Inactiveへ置換しません。systemdだけ通信不能ならExecutionはunknownで、取得できたDockerの事実は別に表示します。Coreに保存済みの観測値を復元して正常と扱うこともしません。

内部観測の切断・復旧は重複を抑えたイベントにします。復旧時は新しい基準とし、切断中の子プロセス遷移を推測しません。Health異常やコンテナ実体変更も記録しますが、修復操作は一切発行しません。

## ReadinessとApproval

Manual Gateは開発者による続行許可です。GUIの`Continue`は既存のapprove要求だけを送り、サービスのStartやHealth判定は行いません。Restartで承認を保持し、Reinitializeでリセットする仕様、boot ID付き永続化、Cancelの意味は維持します。

`http` / `container_ready`は互換性のため残します。後者はsystemd WorkloadのDocker Observerにも指定でき、シーケンスごとに現在のHealth/Runningを再取得します。個別Start/Restartは従来どおりGateを迂回します。ObserverエラーをReady成功として扱いません。

Groupは開発中の段階的な操作範囲・順序を管理します。実行用DAG、自動再起動、望ましい状態への修復は追加しません。systemd依存InspectionはLifecycleのunitだけを解析し、Docker Observerを指定してもManual Gate迂回の診断を維持します。

## GUI

ExecutionとReadinessを別のツリー列にし、Workload選択時にHealth、Lifecycle Controller、Internal Observer、Child Controllerを表示します。内部観測失敗時も親のStart/Stop経路は変えません。Core切断中はstaleと明示し操作を無効化します。

GroupのProgressタブは、正順Start・逆順Stop・Restartの両フェーズ、完了済み、承認/Ready待機、次のStep、Failed/Skippedを表示します。未実行をactive状態からcompletedと推測しません。部分Groupの操作を親Group全体の完了と表示せず、親は全順序のプレビューと実際の操作範囲を示します。同名のWorkload/Gateでも進捗を区別します。Parallelの内部は同時実行で、次のStepへ進む前に全結果を確認します。

## Docker Healthをsystemd起動完了へ委譲する方針

アプリが対応するならType=notifyによるReady通知を優先します。Docker向けには、unitの`ExecStartPost`から実行する[期限付きHealth待機PoC](../examples/systemd-docker-health/wait_health.py)と[unit例](../examples/systemd-docker-health/robot-container.service)を追加し、実際のStartジョブ・失敗・Cancel・Stop/Restart・GUI Continueを検証しました。[実測結果と制限](health-start-verification.md)を参照してください。

helperは起動時だけDocker Healthを読み取り、期限切れ・消失・検査不能・Health未設定を成功と扱いません。失敗後にコンテナを停止する方針はunitのExecStopPostに明示します。常駐監視・独自Supervisorは追加せず、unitの編集・適用は利用者が行います。

参照：[systemd.service（TypeとExecStartPost）](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[Docker Engine API](https://docs.docker.com/reference/api/engine/version/v1.45/)。

## 検証と制約

全237ケースが成功しました。Ruff、サンプル設定のvalidate、wheelビルドも確認しています。

Fake/Docker API stubでLifecycleの操作先、HealthとExecutionの分離、Supervisor操作、切断・タイムアウト・復旧、Core再起動、Gate継続仕様、Inspectionを検証します。既存のBridge互換性・epochテストを維持します。実Tk GUIと、専用の一時user unitからDockerコンテナを起動する統合テストも含みます。既存サービス、linger、自動起動設定は変更しません。

Docker内部は既存の定期観測です。全イベントの監査や不明期間の再構成はしません。ホストunitとDocker selectorの対応誤り、別selectorによる重複管理は自動保証できません。ROS 2の実環境を必要とするBridge固有テストは既存の別テスト手順を使用し、今回ROSのプロセスを起動する機能は増やしていません。
