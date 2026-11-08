# systemd起動ジョブ・Docker Health・Manual Gateの実動作検証

2026-11-08、Ubuntu 24.04 / Python 3.12.3 / systemd 255.4-1ubuntu8.18で検証しました。
専用イメージ`devdash-supervisor-test:4.3.0`と、一意な`devdash-py-it-*` runtime user unitだけを使用しています。既存サービス、linger、常駐Coreの設定は変更していません。

## 検証した構成

[unit例](../examples/systemd-docker-health/robot-container.service)、[unit側のHealth待機PoC](../examples/systemd-docker-health/wait_health.py)、[devdash設定例](../examples/systemd-docker-health/devdash.yaml)を追加しました。Core、GUI、Steps実行エンジンへの機能追加は不要でした。

```text
systemd: docker start --attach
  └─ ExecStartPost: Docker Healthがhealthyになるまで期限付きで待機
       └─ systemd Startジョブ完了（JobRemovedのdone）
            └─ devdash Workload Step完了
                 └─ Manual Gate: GUIで観測を確認してContinue
                      └─ devdashが次のsystemd unitのStartを要求

Docker Observer: コンテナのHealth・OSプロセス・Supervisorサービスを別に取得
```

**devdashのhttp / container_ready Gateは使用していません。** GroupのStepsは`container → Manual Gate → next`だけです。
必須Ready待機はunit側、承認はdevdash、内部観測はDocker/Supervisorという分担を、実際の起動処理で検証しています。

DockerのHealthcheckはテスト用の条件ファイルで制御します。待機中のstartingを観測してから条件を満たし、healthyになったことを確認します。実Tk GUIのStart/Continueボタン、Socket IPC、実systemd/Dockerを通したテストもあります。

## 正常系・停止・再起動の結果

- Healthがstartingの間：unitは`activating / start-post`、Startジョブは保留中、devdashのWorkload Stepはrunning、Manual Gateには未到達です。nextはinactiveです。
- healthy到達後：Startジョブの`done`を受けてStepがcompletedになり、Manual Gateへ進みます。Docker Observerからhealthy・生OSプロセス・Supervisorの`worker:worker=RUNNING`を取得できました。
- GUIのContinue後：初めてnextへのStart要求が発行され、nextがactiveになりました。Continue自体は承認だけを送ります。
- Group Restart：`next Stop → container Stop → container Start（再度Health待機）→ next Start`。Manual Gateの承認は再利用され、コンテナの起動世代は変わりました。
- Group Stop：`next Stop → container Stop`。Gateは実行せず、両unitとコンテナが停止しました。
- Health待機中にdevdashから別のGroup Stop/Restartを要求すると、既存仕様のbusyエラーで拒否されます。実行中操作を暗黙に置換しません。中断したい場合はCancelで未実行Stepを止め、発行済みジョブの終了後に次の操作を行います。

## 境界条件の実測

`JobRemoved.result`と`Service.Result`は別の情報です。特にserviceの起動タイムアウトを、そのままジョブ結果の`timeout`と仮定できませんでした。

| 条件 | 追跡したStartジョブの結果 | Service.Result | devdash / 後続 |
| --- | --- | --- | --- |
| healthy到達 | `done` | `success` | Manual Gateで待機、Continue後にnext Start |
| Healthcheckがunhealthy | `failed` | `exit-code` | Failed、後続Skipped |
| Health待機PoCの期限切れ | `failed` | `exit-code` | Failed、後続Skipped |
| systemdのTimeoutStartSec到達 | `failed` | `timeout` | Failed、後続Skipped |
| Health待機中にコンテナをkill | `failed` | `exit-code` | Failed、後続Skipped |
| devdash Cancel後にhealthy到達 | `done` | `success` | Cancelling → Cancelled、Start StepはCompleted、GateとnextはSkipped |
| devdash Cancel後にコンテナをkill | `failed` | `exit-code` | Failed、発行済み操作の失敗を保持、後続Skipped |
| 外部systemctl Stop | `canceled` | `signal` | Failed、後続Skipped、コンテナ停止 |
| 外部systemctl Restart | 元の追跡ジョブを保持して最後に`done` | 最後は`success` | Manual Gateで待機、nextは未起動 |

### Cancelはsystemdのジョブ取消ではない

Cancel要求への応答は、ジョブ終了を待たずCancellingを返します。要求の前後で追跡中のsystemdジョブpathが維持され、Coreはその結果を待ちました。成功後もコンテナはactive/healthyのままです。devdashからStopやDocker Lifecycle操作を追加発行していないことも検証しています。

Cancel中でも、発行済み操作が失敗したときは最終結果をFailedにします。Cancelledで失敗を隠しません。Cancelは単に「後続を実行しない」指示で、発行済みStartが完了する期限はunit / systemd側の設定です。

### 起動失敗後のコンテナ停止はunitの責務

ExecStartPostで失敗した場合、ExecStopが呼ばれる前提にはできません。例ではExecStopPostにも`docker stop`を置き、Health失敗・タイムアウト・起動中断時に停止する方針を明示しました。

失敗系ではdevdashが要求したLifecycle操作は最初のStartだけで、Stopは要求していません。それでもコンテナが停止したことから、unitの終了処理が働いたと確認できます。これはdevdashのロールバックではありません。ExecStopPostなしのunitについて、同じ停止を保証するものではありません。

### 外部Restartは必ずしもStartジョブを取り消さない

今回のsystemdでは、外部Restartが元のStartジョブを保持したままコンテナを再起動し、再びHealthを満たすと、その追跡ジョブがdoneになりました。「外部Restartなら必ずcanceledになる」とは扱えません。

devdashは受け取ったジョブ結果を追跡し、元のシーケンスが生きているので、その後Manual Gateへ進みます。新しいシーケンスを自動再開・再発行したのではありません。今回は未承認Gateがあるためnextは開始されず、最後にCancelして終了することも確認しました。既に承認済みのGateは既存仕様どおり再利用されるため、外部操作の検出を強制停止機構と見なさないでください。

systemdの版・unit定義・要求のタイミングでジョブ統合の結果は変わり得ます。テストは元のジョブがcanceledになる場合も、doneとなって未承認Gateに進む場合も、観測した結果と後続未実行を確認します。

## PoCの範囲と使い方

`wait_health.py`はstdlibだけで動く、一回限りの読み取り用probeです。`docker inspect`で既存コンテナを確認し、healthyで成功、unhealthy / Healthcheck未設定 / 検査不能 / 期限切れ / 待機中の実体変更・再起動で失敗します。Start/Stop/Restart、コンテナ作成、継続監視、自動修復は行いません。

Type=execのプロセス実行直後には、docker startがまだEngineへ接続している場合があります。その短い期間はRunningへの遷移も期限内で待ち、一度Runningを観測した後の終了・再起動は失敗とします。画像やROSノードの品質を判定するprobeではありません。

本番に使う場合は、既存コンテナをそのunitが単独で所有すること、実際のパス、Docker socketの権限、Healthcheckの猶予期間、TimeoutStartSec/TimeoutStopSec、失敗時のExecStopPost方針を確認してから、利用者自身がGit管理して配置してください。例は自動インストールされず、wheelにも常駐Coreの機能として組み込みません。system unitで使う場合もGUI/Coreをrootで動かす必要はありません。認可は従来どおりsystemd/Polkitへ委譲します。

**実機の画像・センサ品質を人が確認する部分は未検証です。** テストではHealth条件とSupervisor状態を確認してContinueする導線を検証しました。実センサの表示・判断は実環境で行ってください。また、healthyは起動時の観測値であり、起動後ずっと正常である保証ではありません。

参照：[systemd.serviceのExecStartPost / ExecStopPost](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)。

## 再実行

追加19ケース（probe単体9、実systemd/Docker9、実GUI1）を含む全237ケースが成功しました（103.80秒）。Ruff、設定例のvalidate、wheelビルドも成功しています。PoCはwheelへ自動導入されません。

既存の専用fixtureイメージを使用します。必要ならREADMEの手順でビルドしてください。

```bash
# probeの単体テスト（実systemd/Docker不要、時計を制御）
.venv/bin/pytest -q tests/python/test_health_probe.py

# 実systemd/Dockerの境界テストと、実GUIのContinueまで
DEVDASH_GUI_TEST=1 DEVDASH_SYSTEMD_TEST=1 DEVDASH_DOCKER_TEST=1 \
  .venv/bin/pytest -q tests/python/test_health_start.py tests/python/test_health_start_gui.py

# 必要なら専用unitだけの観測結果をJSONで保存（既存ファイルには追記）
DEVDASH_HEALTH_REPORT=/tmp/devdash-health-evidence.json \
DEVDASH_GUI_TEST=1 DEVDASH_SYSTEMD_TEST=1 DEVDASH_DOCKER_TEST=1 \
  .venv/bin/pytest -q tests/python/test_health_start.py tests/python/test_health_start_gui.py
```

タイミングを固定sleepで推測せず、実際のstart-post / Health / ジョブ / Gateを確認して次の操作を行います。外部systemd/DockerとTkの変化を検出するための短いポーリングは使用します。テスト終了時は専用unitとコンテナだけを清掃します。
