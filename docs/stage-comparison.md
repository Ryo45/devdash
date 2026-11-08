# Coordinator方式とStage方式の比較

比較元はmain **`d143e97`**（Python/ttkbootstrap、Core IPC 1）、代替実装は`feature/staged-startup`（0.3.0.dev0、Core IPC 2）です。既存のmain・リリースタグは変更していません。このブランチを完成させることと採用・マージは別の判断です。

## 完成した代替方式

選択StageのStart/Stop/Restart、明示Next、個別Workload操作、Child Controller、監視、ログ、イベント、依存Inspection、CLI、GUI、設定Editor、Generator、旧設定の移行を提供します。

CoreにGroup/Step/Gateの互換実行系を残していません。旧形式は読み込み時に拒否され、オフラインの一方向移行ツールだけが旧スキーマを理解します。GUIのNextは選択Stageから決定する次のStart要求です。受付後の選択表示は対象Stageへ移りますが、これはクライアントの選択状態で、Coreの永続カーソルではありません。

操作結果は現在/最後の一要求分を保持します。全Stageの過去の成功をラッチしません。別Stageを選んだとき、現在のWorkload状態は見えますが、その状態だけで過去のStage操作が成功したとは推定しません。

## Runtime Coreの変化

削除したもの：

- 手動承認API、承認集合、boot ID付きファイル読込・永続化。
- Reinitialize、Restart時の承認保持・リセット規則。
- Gate待機タスク、承認通知用Event、HTTP/container_readyの起動ポーリング。
- Group階層展開、Stop逆順/Start正順の複数フェーズ実行。
- 未承認Gateの稼働不一致・Gate迂回診断。

追加したもの：

- Stageの独立した一バッチ要求と、メンバーごとのCompleted/Failed/Skipped結果。
- 設定順から対象を決める明示Next。後続の自動発行・復旧・再試行はなし。
- 明示依存のStage順・manager・Lifecycle整合性検証と、未適用drop-inの診断。
- 編集元とRuntimeの分離に必要な明示Reload。古い観測・結果を持ち越さない。
- 設定再読込後に古いGUIが異なるNext対象を操作しないための一時的なconfig revision。Core instanceと組み合わせ、永続化しません。承認や進行位置を表す状態ではありません。

Lifecycle実装は引き続きsystemd/Dockerです。Stage Restartは対象のBackend Restartを発行します。全体のStop→Startワークフローは実行せず、全体停止が必要なら利用者がStage逆順に明示操作します。部分失敗では、独立した同一Stageの他メンバーも結果まで追跡し、成功分をロールバックしません。

systemdのJobRemoved、返信前の完了通知、bus owner世代、未確定結果、再接続、Polkit理由、Scope区別を再利用しました。Dockerの安定selector解決・リソース/プロセス観測、Supervisor/ROS Bridgeの能力・epoch・互換性検査も再利用しました。systemdのNamesをalias診断へ利用する点だけを追加しています。

## オフライン側へ移った複雑さ

Editorはフォーム・選択・Sourceの未反映変更を扱います。Generatorは所有ファイル、内容hash、差分、古い生成物、原子的なcurrent切替を扱います。これらはWorkloadを生存させる状態機械ではなく、Gitでレビューする設定成果物を作る処理です。

この追加は無料ではありません。Coreが単純化しても、Editor/Generatorを含めたアプリ全体のコード量は増えました。既存unit本体まで生成し直す仕組みやホスト適用デーモンは追加せず、同じConfigと生成関数をCLI/GUIで共有しています。

行数は意味的な複雑さの尺度ではありませんが、比較元commitとこのブランチをAST/ファイル単位で照合した参考値です。操作系の数値はEngineの初期化、busy、Cancel、launch、track、execute、旧persist/approve/run_step/gateを対象とし、観測・診断・snapshotを除いています。

| 区分 | Coordinator | Stageの実装方針 |
| --- | --- | --- |
| Engine操作系メソッド | 約259行 | 約140行。一バッチとCancel/結果のみ |
| engine.py全体 | 550行 | 約410行。観測・診断の多くは維持 |
| model.py | 355行 | 約350行。旧階層の代わりにStage/明示依存/HTTP観測検証 |
| inspection.py | 214行 | 約250行。実行はせず、既存依存と生成宣言を診断 |
| 新Editor | なし | 約485行。フォーム・Source・生成preview |
| 新Generator / Migration | なし | 約210 / 115行。ファイル安全性と一方向変換 |

LOCを減らす目的で依存InspectionやIPC世代・未確定結果の安全性を削っていません。逆に、全runtime codeを一括削減できたという評価でもありません。

## 9シナリオの操作比較

操作回数はLifecycle/進行要求ボタンの数で、対象選択・画像ビューアー操作は別です。mainの既定サンプルはSensors後に一つのGateだけなので、認識確認後にも停止させる比較には旧側へ二つ目のGateを設定する必要があります。

| シナリオ | Coordinator | Stage |
| --- | --- | --- |
| 1 Sensorsを起動 | Robot Start 1回、内部Stepsが進む | Sensors Start 1回、ここで一操作が完了 |
| 2 Camera/IMU確認 | Gate待機中に観測 | Stage完了後に観測。ラッチは不要 |
| 3 Perception起動 | Continue 1回、既存実行タスクを解放 | Next 1回、新しいStart要求。任意に直接Startも可 |
| 4 認識結果確認 | 二つ目のManual Gateが必要。ない場合Controlまで自動進行 | 操作なしでも後続は止まっている |
| 5 Control起動 | 二つ目のContinue 1回 | Next 1回。3段階とも比較条件を揃えれば計3要求 |
| 6 一部Restart | 対象Group Restart 1回。逆順Stop→Startと承認再利用を理解する | 対象Stage Restart 1回。各Backend Restartとsystemdの伝播を理解する |
| 7 Coreが落ちる | Workloadは維持。途中sequenceとGate待機は失うが承認ファイルは残る | Workloadは維持。操作結果を失うだけで承認/カーソルは存在しない |
| 8 復旧後の必要操作 | 現状と承認保持を見てGroup/個別操作を選ぶ。途中再開なし | 現状を観測し、必要Stageを直接Start/Restart。Active Startは不要なRestartをしない |
| 9 Gitで順序・依存変更 | devdash YAMLとunitを別に編集・検査 | 一編集元を編集→差分→Generate→drop-in手動適用/reload→Core Reload。ファイル手順は増えるが二つの生成物は同じモデル由来 |

Stage版は「確認したので続行する」という承認状態を保存しません。したがって普通の再起動で承認を再利用するか・Reinitializeでどこまでリセットするか、という運用判断がなくなりました。一方、複数Stageを一回で再実行する利便性は意図的に失っています。

## 実環境で確認した境界

- Stage Start後に後続は起動せず、GUIのNextを押して初めて次のsystemd unitが起動します。
- 生成したRequisite/Afterを専用unitへ明示適用し、前段Inactiveで後段Startがdependency失敗することを確認しました。前段は自動起動されません。
- 前段がunit側のExecStartPostでDocker healthyを待ち、devdashはStartジョブ完了だけを追跡します。Observerは同時に内部プロセス・Health・Supervisorを観測します。
- 待機中Cancelはジョブを取消しません。成功ならStage結果CancelledでもWorkloadは稼働したまま、失敗ならFailedを保持します。後続Stageは常に別要求です。
- Stage Restartは対象だけ、ActiveへのStartでInvocationIDは変わらず、CoreのSIGTERM/SIGKILL/常駐サービス再起動でもWorkloadのPIDは維持されます。
- **RequisiteでもStageは操作範囲の完全な隔離境界ではありません。** 前段Stopが後段へ伝播することを実測し、事前Inspectionでoutside_stopを表示しました。安全な全体停止には逆順の明示操作が必要です。
- 外部Restartは保留中のStartジョブへ統合される場合があります。受け取った実ジョブ結果を追跡し、勝手に新しいStageを発行しません。
- 必須依存のRequisite/Afterが既存unitのWants/Requiresなどを消すわけではありません。後段への起動伝播・同一Stage依存・逆行・循環・Conflicts等は診断対象です。

## 既存テストの移植

旧テストを通すための互換Group/Gate実行系や一括skipは残していません。機能廃止で意味を失うテストは、新しい安全性の保証へ置き換えています。

| 旧保証 | Stage側の対応 |
| --- | --- |
| 階層展開・正逆順・Group再起動 | Stage独立範囲、並列結果、Backend Restart、明示逆順停止の実テスト |
| Gate承認・boot保持・Reinitialize | Coreに承認API/ファイルがないこと、Nextまで後続未起動、再起動後履歴なし、移行時の確認事項/警告 |
| HTTP/container_ready待機 | HTTP/Dockerの観測と実行状態の分離、unit側Health待機の成功/失敗/期限切れ |
| Cancel・途中失敗・ロールバックなし | 即時Cancel、検査中Cancel、発行済みのdrain、部分成功、Cancel後の失敗保持、unit/Docker/Controller解決中なら未発行要求を抑止 |
| 競合防止・更新中再読込 | 一つの操作制限、Child操作との競合、Reload直前再確認、古いpoll破棄、旧config revision操作拒否 |
| systemd依存・迂回 | Stage前倒し、順序、同一Stage/循環、宣言未適用、未知unit経由、alias、停止伝播、reload後診断 |
| D-Bus/IPC/認可/世代 | 返信前完了、owner交換、bus再接続、burst、Polkit拒否、同名manager区別、handshake固定、結果不明の再送禁止を維持 |
| Scan/Import | 読み取りScan、Source Import、生成/ReloadまでCore無変更、自己管理防止 |
| GUI/Core常駐 | 実TkのStage/Next/Editor、通信断復旧、閉じても操作継続、実user serviceでCore再起動後PID維持 |

新規のGeneratorテストは再現性、diff、不要ファイル検出、手編集/未知ファイル/symlink/manifest衝突、生成失敗後current維持、Sourceの古い保存拒否、生成Runtimeの直接編集拒否を検証します。移行テストはWorkload保存、並列/逐次境界、manual確認、HTTP/container条件保持とack必須、循環/重複拒否を確認します。

## 検証結果

2026-11-09の最終実装で **242ケース成功（106.39秒）**、Ruff、全サンプルのモデル検証、wheelビルドを確認しました。さらにwheelを別venvへ導入し、ソースディレクトリ外からCLIのvalidate / generateと、Editor/Generator/Migrationのimportを確認しています。Core IPCは2、Bridge IPCは1です。

比較元mainでは237ケースが成功していました。数の一致は目標にせず、上記の保証ごとに移植しています。Gate・永続化そのもののテストは新仕様では不適用ですが、後続未起動、結果不明、再起動時の非再実行、移行時の条件消失防止へ置き換えました。追加したCancel境界は疑似D-Busでunit情報取得中に止め、StartUnitが発行されないことを直接検証しています。Docker解決中とController snapshot中も同様です。

再実行はREADMEの全テストコマンドを使用します。テスト対象は専用の一時unit/コンテナだけで、既存サービスやlingerは変更していません。実画像・ROSノード品質を確認したという意味ではありません。

## 採用判断と残課題

Stage版はRuntimeを小さく理解しやすくし、「どの操作で次を起動するか」が明確です。開発中に毎段階で観測したい用途には適しています。必須依存と起動Readyをunit側で整理できることが前提です。

Coordinator版は一つのGroup操作を複数段階の手順として実行したい場合に便利です。その代わり、タスクと承認の寿命・保持規則・再初期化・外部依存との不一致を理解する必要があります。どちらが運用全体で単純かは、unitをどこまで整備できるかと、反復する操作の粒度に依存します。現時点でmainの採用方式を変更する判断はしていません。

残る論点：

- 生成drop-inのホスト適用・削除・rollbackは手動です。自動適用デーモンを作らず、運用手順として許容できるか。
- systemd既存依存の全トランザクションは再現しません。間接作用と動的トリガーは警告/検査範囲外です。Editorのライブ検査も点時点の情報です。
- Activeは現在Healthの保証ではなく、健康異常でもdevdashは自動修復しません。必要な継続的安全性は下位実行主体と実機側の責務です。
- Dockerの異なるselectorが同じ実体を指す場合の静的重複検出は完全ではありません。PIDを論理サービスIDとして操作しません。
- GUIはフォームのApplyを前提とする最小Editorです。依存関係を跨ぐ大きな再構成はYAML編集の方が適する場合があります。人を対象にした操作性評価はまだ行っていません。
- 実画像・センサ品質判断、ROSディストリビューションごとの実Bridge動作は今回のGUI試験で再評価していません。既存Bridge互換性/epochテストと、ROS用の独立実機検証手順を維持しています。

次の比較は同じロボット構成・同じunit定義で行い、上記9シナリオの操作ミス、理解に要する説明、反復時の手数を評価するのが適切です。このブランチはそのまま起動・編集・生成・テストできる比較対象として残します。
