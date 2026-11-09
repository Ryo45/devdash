# Stage方式とOn-demand Plan方式の比較

これは`ec834d5`時点の記録です。追加の現在状態Startupは[別レポート](start-missing-comparison.md)にまとめています。

比較元は`feature/staged-startup`の **`b6cad2a`**（0.3.0.dev0 / Core IPC 2）。この実装は`feature/on-demand-operation-plans`（0.4.0.dev0 / Core IPC 3）です。main `d143e97`とStageブランチ、既存タグは変更せず、マージしません。[実装前の責務・寿命設計](on-demand-design.md)に沿った比較用の実装です。

## 実装と依存方向

| 部分 | 実装・所有する状態 |
| --- | --- |
| `observer.py` | 常時観測、Lifecycleと内部観測の別々の損失・復旧基準、200件のイベント、鮮度、Inspection結果。Plan/Operatorへの依存なし |
| `operator.py` | 唯一のbusy・Cancel・実行Task。単発/Stage/Child要求と結果追跡。共通`batch`をPlanにも提供 |
| `plans.py` | 標準3種類の有限な手順生成、明示preview、Run中の進捗・Checkpoint。Lifecycle発行はOperatorへ委譲 |
| `engine.py` | 上記を組み立てるCore facade。イベント配送とIPC snapshot。旧Engineの観測・操作実装を重複保持しない |
| GUI / CLI / IPC | 通常操作とPlan操作を分離。preview後だけExecute。Continue/AbortをRun IDへ結び付ける |
| Editor / Generator | Stage側の実装を維持。Plan Templateを設定として保持するだけで、Runtimeからimportしない |

OperatorはObserverのキャッシュを成功判定に使いません。Stageの事前検査には注入されたInspection関数を使いますが、これはsystemdメタデータの診断です。実行結果の正本はAdapterが追跡したsystemd JobRemoved、Docker/既存Controllerの応答です。Observer停止・取得失敗で成功へ変換しません。

systemdの接続owner固定、早着JobRemoved、manager lease、Polkit、manager別の識別、自己管理防止を維持しました。InvocationIDのPropertiesChanged購読を追加し、保留中Startに外部Restartが統合されるケースを検出します。実ジョブの`done`と「要求中に複数世代が混ざったため操作の結果が不確実」を別々に記録します。操作元を外部ユーザーだとは断定しません。

DockerのLifecycle/内部Observer分離、安定selector、Supervisor/ROS Bridgeの能力・epoch・互換性、ログを再利用しました。Bridge本体・IPC 1は変更していません。

## 状態の寿命と増えた複雑さ

Planを使わない通常操作ではactive RunもCheckpoint待機Taskも作りません。完了したPlanは設定snapshot・lease・確認tokenを解放し、Operatorの最後の結果に表示用要約だけを残します。次の単発操作でこの要約も置き換わります。承認ファイル、boot ID、Reinitialize、現在Stageの永続カーソル、再開機能はありません。

TemplateはYAML上の静的定義です。previewはCore内に最大8件、Executeで使い切り、Reload/Core再起動で破棄します。Runは一回限りのUUID・設定コピー・有限なsteps・一つの現在Checkpointを持ちます。busyとCancelを別に実装せず、Operatorの一枠を使います。

Checkpointの確認はRun IDと表示tokenに限定します。Continue時に再観測し、世代・Health・内部Controller・不明/鮮度が変化したら古いtokenを拒否します。関係するmanager leaseを失うとRunを失敗させ、自動継続しません。不明な観測を明示したまま人間が確認することは許します。これは必須Readyや安全インターロックではありません。

**Stage版よりRuntimeのコード量・一時状態は増えました。** 省略せず評価すべき追加コストです。

| 参考値（空行・説明を含むファイル行数） | Stage | On-demand Plan |
| --- | ---: | ---: |
| Core facade / 旧Engine | 410 | 157 |
| 独立Observer | Engine内 | 294 |
| 独立Operator | Engine内 | 214 |
| Plan生成・実行 | なし | 342 |
| 上記Runtime合計 | 410 | 1,007 |
| model | 351 | 373 |
| Inspection | 254 | 254 |
| Editor / Generator | 487 / 210 | 487 / 210 |

増分には抽出した責務の境界、観測チャネルの独立復旧・鮮度、ジョブ文脈の追跡強化も含みます。単に旧Engineが小さくなったので全体が単純になったとは評価しません。一方、増えた確認状態はRun中に限定され、ロボット稼働期間・OS boot・通常Restartへ寿命を広げていません。汎用DSL、実行DAG、条件分岐、再試行、Supervisor、イベント永続化は追加していません。

オフラインEditor/Generatorの複雑さは今回増やしていません。Generatorの世代管理を簡素化する変更は独立した検討事項です。生成物を使わない設定でも、単発操作・Plan生成/実行は利用できます。YAMLの明示依存が実unitに適用済みかのInspectionは引き続き必要です。

## 操作比較

ボタン数は対象選択・画像確認・外部ビューアー操作を含めません。SensorsとPerceptionにchecksがある同じ設定を比較します。

| シナリオ | Stage | On-demand Plan |
| --- | --- | --- |
| 日常のPerception Restart | 対象選択→Restart 1回 | 同じ1回。Plan作成不要 |
| 初回Sensors→確認→Perception→確認→Control | Start / Next / Nextの3回。各操作でCore枠を解放 | Preview / Execute / Continue / Continueの4回。順序と対象を最初にまとめて確認 |
| 画像・IMUと認識結果の観測 | 通常画面で観測 | 同じ通常画面。Checkpointタブにも現在状態と確認事項を表示 |
| Perception以降の再起動 | Control Stop→Perception Stop→Perception Start→Control Startの4要求 | Preview / Execute / Continueの3回。逆順Stop→正順Startを手順として展開 |
| Perception以降のStop | Control / Perceptionの2要求 | Preview / Executeの2回。対象をまとめてレビュー可能 |
| 途中で開発を中断 | 現在の要求だけCancel | Abort Plan。発行済み結果を待ってから自由な通常操作へ。開始済みWorkloadを停止しない |
| GUIを閉じる | Coreの要求は継続 | Run/CheckpointもCore内で継続。再接続で現在のRunを表示 |
| Coreが落ちる | Workload維持。操作履歴を失う | 同じ。Run/preview/確認も失い、成功推定・自動再実行なし |
| 復旧後に必要な箇所だけ操作 | 再観測して個別/Stage操作 | 同じ。以前のPlan位置は操作を制限しない |
| GitでStage・明示依存を変更 | 編集→生成diff→明示適用→Core Reload | 同じ。Templateは任意。保存/生成だけで実行しない |

Planは初回起動のクリック数を減らす機能ではありません。最初に全体をレビューできること、suffix操作を毎回組み直さず実行できることが主な利点です。checksが空の境界は自動で通過し、最後にはCheckpointを置かない仕様をpreviewに明記します。常に段階ごとに自由に操作したい場合は通常のStage/Nextを使えます。

## 検証

Ubuntu 24.04 / Python 3.12.3 / systemd 255、実Tk画面と専用Docker fixtureで検証しています。

| 保証 | 自動テスト |
| --- | --- |
| previewは無操作、Templateは静的、順序・scope、空checks、単発操作から自由に戻る | `test_plans.py` |
| Checkpoint待機、再実行で確認再利用なし、Cancel drain、Stopフェーズ中止、部分失敗・非rollback | `test_plans.py` |
| 世代変更・古いContinue・manager loss、Core復旧でRun/previewなし、Observer不調とジョブ結果分離 | `test_plans.py`, `test_plan_ipc.py` |
| CLI preview/cancel、実Unix Socket、Run ID/設定世代/プロトコル拒否 | `test_plan_ipc.py`と既存IPC/バージョンテスト |
| GUI preview→Execute→二つのCheckpoint→Continue、再実行、GUI閉じても継続、中止後Restart | `test_plan_gui.py` |
| SIGTERM / SIGKILL、実Coreプロセスのジョブ中・Checkpoint中終了、再起動後の観測のみ | `test_resident.py`（隔離した疑似D-Bus manager） |
| 実systemd StartがDocker Healthを待つ、内部プロセス/Supervisor観測、Checkpoint後だけ後続Start | `test_plan_health.py`, `test_health_start_gui.py` |
| 実suffix Restart、Active StartがRestartしない、Cancelで実ジョブ維持、外部Restart統合で進行拒否 | `test_plan_health.py` |
| 切断直後はLifecycle不明、独立Docker状態は維持、再接続だけで正常復元しない | `test_plans.py`と既存再接続テスト |
| user/system区別・認可、systemd依存Inspection、生成Requisite拒否、Docker Health失敗/期限 | 既存テストを維持 |
| Supervisor・ROS Bridgeの能力/epoch/非互換拒否、Generator安全更新・移行、GUI Editor | 既存テストを維持 |

2026-11-09、全体 **275件成功（149.42秒）**、その後追加したStopフェーズ中Cancelと実Checkpoint中外部Restartの **2件成功（13.52秒）**。合計277件の保証を確認しました。全体実行の対象は実GUI / systemd / Dockerを含み、Skipや失敗はありません。追加はテストのみで、製品コードは同一です。

Ruff check / format、8つのサンプル設定の検証、wheelビルドに成功しました。wheelを別venvへ固定依存とともに導入し、ソースディレクトリ外から`python -I`でCore/GUI/Editor import、Template preview、設定生成を確認しました。全テストはREADMEのコマンドで再実行できます。

テストは専用の一時user unit / コンテナだけを変更し、既存サービス・linger・実system managerの重要サービスは操作しません。ROSの物理環境を新規に起動して検証したという意味ではなく、既存Bridgeプロトコル試験と独立検証手順を維持しています。

## 制限と採用判断の論点

- Planは要求順序であり、systemdの全トランザクションや停止伝播の隔離境界ではありません。Inspectionの未解析範囲は残ります。必須Readyと継続安全性は下位の実行主体の責務です。
- 外部操作元を完全には特定できません。ジョブと観測世代の対応で不確実性を検出しますが、すべての短い状態変化やDocker内部の外部操作を完全に捕捉する保証はありません。
- 状態が頻繁に変化するとContinueが再確認を求める場合があります。CPU/メモリや生のOSプロセス一覧の変動は確認fingerprintから除外しています。ジョブ結果の正本にはしません。
- 実行枠は従来同様グローバル一つです。Checkpoint中に別操作したいときはPlanを中止して通常操作へ戻ります。非競合操作の並行化は未実装です。
- 最後の結果はCore内表示用だけです。Core停止時のRun再開や永続監査履歴はありません。
- Templateは標準3種の開始点の保存だけで、GUIの自由手順Editorは追加していません。Stage/checksは既存Editorで編集し、名前付きTemplateはYAMLで編集します。
- センサ画像・認識品質を含む人間の操作性評価はこれからです。自動テストは実際のGUIと操作境界を検証しますが、実ロボット運用の容易さを証明するものではありません。

日常の単発操作はStage版と同じ導線です。初回起動とsuffix再起動を繰り返す用途にはPlanが有用ですが、RuntimeはStage版より大きくなっています。判断すべき点は「Run限定の確認・進捗を持つ追加コストに、まとめてレビュー・実行できる便利さが見合うか」です。旧Coordinatorのboot単位承認へ戻る必要はなく、採用前に同じ実機・同じunitで上記の操作を比較できます。
