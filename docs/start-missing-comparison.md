# Startup再実行と現在状態One-shot Startupの比較

起点は`feature/on-demand-operation-plans` / **`ec834d5`**。実装ブランチは`feature/state-based-startup`、パッケージは0.4.0.dev1 / Core IPC 3 / Bridge IPC 1です。main・既存検証ブランチ・タグは変更せず、マージしません。

## 振る舞い

| 現在の観測 | start_missingの判断 |
| --- | --- |
| Inactive / Failed | Startを一度要求。StartLimit等の拒否・ジョブ失敗はそのまま表示 |
| Active、提供されているReady/Health条件を満たす | Skip。Lifecycle要求なし |
| Active、Readiness/Health契約が未提供の通常systemdサービス | Skip。ただし品質Readyの保証ではなく、checksはそのまま残す |
| ActiveだがUnhealthy / Not Ready | Block。Restartで直そうとしない |
| 設定されたDocker / HTTP / systemd情報の取得不能 | Block。契約未提供と取得失敗を区別 |
| Unknown / Stale / Not Found / 遷移中 | Block。自動Start/Skipせず再観測・明示対応を要求 |

HTTP/Docker等の独自Ready判定は追加していません。既存Adapterが返すReadiness/Healthを利用します。systemdのServiceプロパティ取得に失敗した場合は、ActiveというExecutionを維持しつつ、Readinessを「取得不能」として返すよう修正しました。Typeが分からないのに「Ready契約が未提供」と推測しません。

Previewは全Stageの予定Start/Skip/Blockと理由を表示し、IPC経由では読み取り再観測も行います。Pythonの同期preview APIは現在キャッシュを使用するため、必要なら呼出側でpollします。いずれもLifecycle要求を発行しません。

Execute時は全対象を再確認し、Blockがあれば最初の要求前に終了します。StageごとにInspection後の発行直前と、ジョブ完了後にも再観測します。前段の起動済み対象も確認し、空checksによる不健全・不明状態の通過を防ぎます。ジョブ成功と観測上のReadyは別であり、「ジョブCompleted、起動後Readiness不足によりPlan Failed」も表示します。

PreviewでStart予定だった対象が別操作で正常Activeになった場合はSkipへ減らします。既にActiveだった対象の停止や世代交代、BlockedだったPreviewの解消は、新しいPreviewを要求します。古い判断からRestartや追加起動はしません。

CheckpointのRun ID / token / fingerprintは既存実装を利用します。全部Skipでも既存checksのCheckpointを実行し、次のRunには確認を継承しません。Checkpoint中に不健全・不明になった場合、Continueでも通過できません。観測を更新して再確認するか、明示Abortして新しいRunを作ります。manager leaseを失ったRunは失敗終了し、復旧しても再開しません。

## 起動失敗からの復旧

CameraがActive、IMUがFailed、Detector/PlannerがInactiveの例：

| 比較 | 既存startupの再実行 | start_missing |
| --- | --- | --- |
| Camera | Startを要求。systemdでは通常冪等であり、元々Restartではない | Activeと契約を観測して要求自体を省略 |
| IMU | Start | Start |
| IMUが再び失敗 | 発行済み結果を追跡し、後続停止 | 同じ。Cameraは停止しない |
| 修正後の起動 | 新しいRunを作る | 同じ。最新状態から新しい判断を作る |
| 確認 | RunごとのCheckpoint | 同じ。Cameraが以前からActiveでも確認を省略しない |
| 読む過去Run | なし | なし |
| 不健全なActive | Startジョブ結果と既存下位契約に従う | 明示Blockし、不要なStartを出さない |

復旧時のGUIボタン数は減りません。Preview→Execute→Sensors Continue→Perception Continueの4回です。単発修正操作や画面選択は別です。減るのは既にActiveな対象への要求数であり、以前のPlanのどこまで成功したかを利用者・Coreが復元する必要もありません。全てActiveならLifecycle要求0件で二つのCheckpointだけを確認できます。

## 責務と状態の増分

- `start_missing.py`：現在の観測から判断する純粋関数と変更判定。約62行。履歴・I/O・Lifecycle呼出なし。
- `plans.py`：342→約428行。新kindの手順は既存Stage展開を利用。実行前・Stage前後の確認、Run内だけの観測基準を追加。
- `operator.py`：214→218行。Inspection後・発行前の対象選択コールバックを追加。既存batch / track / Cancel / 排他を共用し、単発操作は変更なし。
- `observer.py` / `engine.py`：294 / 157行のまま。Plan進捗や過去成功をObserverへ追加していない。
- Generator / Editor / Bridge：Runtimeの依存は増やさず、既存実装を維持。Templateの新kindを設定・サンプルへ追加。

追加状態はRun内の現在観測基準と、表示用のPreview/実行時判断です。実行前の予想・実際の判断・実ジョブ結果を分離して表示します。Run終了時には実行用基準を解放し、最後の表示レポートだけを保持します。このレポートを捨てても次のRunは動作します。承認ファイル・永続カーソル・Desired State・自動リトライ・Resumeはありません。

## テストと実環境

- 単体：全Inactive、同一Stageの部分Active、Failedの再Start、全ActiveでもCheckpoint、新Runで確認非継承。
- 不明系：Stale / Unknown / 遷移中、Health未提供対Unhealthy、Docker/HTTP契約、Serviceプロパティ取得失敗。
- 競合系：Preview後の変化、Inspection中の変化、Checkpoint後の前段不健全、起動後のReady不足、観測失敗と実ジョブ結果の分離。
- 復旧：IMU失敗後に表示履歴も削除し、現在状態だけから新Runで復旧。Cameraへの要求なし。
- Cancel / manager loss / Core復旧：発行済みを追跡し、後続を止め、Runを再開しない。単発Restartや既存restart_fromのテストは維持。
- 実GUI：Start missingのPreview→Execute→Checkpoint→Continue、Active対象のSkip、Run再実行、中止・GUI再接続後の通常操作。
- CLI / IPC：新kindのPreview、読み取り再観測、Start省略理由、Execute / Cancel。名前付きTemplateでも利用可能。
- 実systemd＋Docker：Camera相当の一時user unitを先に稼働させ、IMU相当コンテナをHealth待機中に終了してStartを失敗させる。新しいRunでIMUのみ再Startし、healthyとSupervisor内部観測を確認して二つのCheckpoint後にDetector/Plannerを起動する。CameraのPID / InvocationID不変を確認。全Activeの次Runでは要求0件でもCheckpointを維持。

2026-11-09の最終全体実行で **314件成功（176.40秒、skipなし）**。実GUI / systemd / Docker、Supervisor / ROS Bridge、Editor / Generatorを含みます。Ruff check / formatにも成功しました。0.4.0.dev1のwheelをビルドし、別venvへ固定依存とともに導入して、ソースディレクトリ外の`python -I`からstate policy / Template preview / GUI・Editor importを確認しています。

DISPLAY / WAYLAND_DISPLAYを外したheadless実行も **286件成功、28件を指定どおり除外（9.64秒）**。8つのサンプル設定を検証しました。異常終了した試験の残留物は所有元を確認して片付け、専用コンテナ・unitファイルが残っていないことも確認しています。

テストのHealth待機期限を2秒にした初期版では、Dockerの次のHealth観測より前に2回目の起動も期限切れになりました。製品の再試行で隠さず、失敗注入を専用コンテナの終了にし、既存の十分なunit側Health待機時間で復旧を検証しました。実テストは一意な一時user unit / コンテナだけを対象にし、既存サービス・lingerを変更しません。ROS実機や画像品質の新たな評価は行っていません。

既存PlanテストでGUI/実統合のマークをimportだけに任せていた箇所も明示再exportへ修正しました。Lintの未使用import除去でopt-inマークが失われず、通常のheadlessテストが実GUI/Dockerを動かさないようにしています。

全体テストでは、GUIの繰り返し開閉後にTk参照がIPCワーカーのGCで回収され、Pythonが異常終了する問題も見つかりました。Window終了時にpending callback・widget・Style参照を解放し、所有スレッドでGCする処理を加えて解消しています。CoreやPlanの寿命は変更していません。最終全体実行はこの修正を含みます。

## 残る境界

観測と操作は原子的ではありません。発行直前の再観測後にも外部操作は可能です。既存のD-Busジョブ・世代診断を維持しますが、全ての短時間変化やDocker内部の外部操作を完全に捕捉する保証はありません。Planはsystemd依存伝播を隔離しません。

再観測は既存Observerの一回のpollを利用し、RunによるHealth待機・自動再試行を追加しません。そのため、ジョブ完了後も下位Ready契約が整っていなければPlanは終了します。必須Readyをジョブ完了に含めたい場合は、unit側のType=notify / ExecStartPost等で保証します。全部SkipのStageも既存Stage Inspectionの対象です。

start_missingは復旧作業の補助です。サービス停止・自動修復をしないこと、品質判断を人間または下位実行主体に残すことが、要求削減より重要な境界です。
