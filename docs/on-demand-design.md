# Observer / Operator / On-demand Plan：実装前の設計

起点：Stage方式 `b6cad2a`。常駐Coordinator・永続承認を再導入しない。

| 所有者 | 責務 | 状態の寿命 |
| --- | --- | --- |
| Observer | 現在のLifecycle/内部観測、イベント、ログ、Inspection用の観測基準 | Core存続中。復旧時は新しい基準。Planを知らない |
| Operator | 唯一のbusy所有者、単発/Stage/Child操作、共通バッチ処理、ジョブ結果とCancelの追跡 | 一要求中。最後の結果は表示用のみ |
| Plan Executor | 設定snapshotから展開済みの操作を順番にOperatorへ依頼、Run限定Checkpoint | Run実行中だけ。終了/中止でsnapshot・Checkpoint・lease・承認tokenを解放 |
| Plan Template | startup / restart_from / stop_from、開始Stageの静的指定 | YAMLに任意保存可。ループや条件式なし |
| Core facade | 接続世代/config revision、API集約、明示Reload | Core存続中。workflow状態は所有しない |

Plan作成はプレビューだけで、Lifecycle要求もbusy占有も行わない。実行時にはプレビューID・Core instance・config revisionを確認し、固有Run IDで一つのOperator枠を予約する。Stage用Engineを再帰実行しない。Planも単発も同じOperator.batchを使い、Plan Executorは独自busyや別のLifecycle追跡を作らない。

標準PlanはStage順から展開する。startupは全Stage Start、restart_fromは後段からStop後に前段からStart、stop_fromは後段からStopのみ。Startの後、最後のStage以外でchecksがある場合だけCheckpointを置く。checksが空なら自動で次の操作へ進むことをpreviewに明示する。確認を必須Readyにしない。

Checkpointは最新観測とfingerprint（manager世代、unit/コンテナ起動世代、内部Controller epoch、取得不能状態）を表示する。ContinueはRun IDとその表示tokenに結び付ける。状況が変わればtokenを更新して再確認を求める。managerが変わった場合は既存leaseを尊重し、Planを正常継続しない。古い観測を成功判定の正本にしない。

Cancel/中止は同じOperatorのcancel eventだけを使う。Checkpointは解除し、発行済みジョブは取り消さず追跡する。終端ではactive Runを捨て、表示用の結果要約だけを最後の操作結果へ残す。次のRunに確認は渡さない。Core再起動後はRun/preview/確認なし。GUI終了はRunを中止しない。

Generatorは任意のオフライン支援。Operator/Plan ExecutorはGeneratorをimportせず、生成物の世代管理を要求しない。Inspectionは診断として再利用し、Plan生成に完全な依存グラフを要求しない。Stage単発操作ではStage版の診断を維持する。

IPCは新しい世代3（Bridgeは1のまま）。設定・Coreの古い世代からの操作と、別Run/古いCheckpointへのContinueを拒否する。

実装後の検証・増加したRuntime状態・操作性の比較は[比較レポート](on-demand-comparison.md)に記載する。Observer/Operator/Planの分離はコード行数を減らす変更ではない。常駐する実行状態を持たず、複数操作が必要な期間にだけ状態を限定することを評価する。
