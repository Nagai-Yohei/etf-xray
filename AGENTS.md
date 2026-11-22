# AGENTS.md — etf-xray

AI コーディングエージェントおよび保守担当者向けの作業ルール。
**作業を始める前に必ず全体と `DESIGN.md` を読むこと。**

- やり取り・コードコメント・ドキュメントは日本語。変数名・関数名・**Web UI の表示文言は英語**。
- 変更は最小限に。変更後は必ず `python -m src.check`（または `./run.sh check`）を通して全項目 OK を確認する。

---

## 最重要：セキュリティ・情報流出防止ルール

1. **秘密情報・個人情報を絶対にコード・設定・生成物・出力ログに書かない**
   - `SEC_USER_AGENT`（氏名・連絡先メール）、GitHub / Cloudflare のトークンや API キーは環境変数または `.env`（Git 除外済み）のみで扱う。
   - `.env` の中身を読み取って画面表示・ログ出力・ファイル転記しない。
   - ローカル環境の絶対パス（`/Users/...`, `C:\Users\...` 等）や個人メールアドレスをコード・コメント・JSON・HTML に含めない。
2. **外部公開・送信操作は自動で実行しない**
   - `git push`、Cloudflare Pages へのデプロイ、外部リポジトリ作成などの操作は、**人間が `git status` / `git diff` と `check.py` の結果を確認したうえで実行する**（エージェントが勝手に実行しない）。
3. **100% 公開データ原則**
   - データソースは **SEC EDGAR** と **OpenFIGI**（および `config/manual_tickers.csv`）のみ。
   - Yahoo Finance などの非公式・再配布禁止 API は追加しない。
4. **フロントエンドの安全原則**
   - 素の HTML / CSS / ES Modules JS のみ（Node.js / npm 依存を増やさない）。
   - 外部 CDN からのスクリプト読み込みは行わず、チャートライブラリは `web/vendor/` にローカル同梱する。
   - XSS 防止のため、外部由来の文字列描画に **`innerHTML` / `outerHTML` / `insertAdjacentHTML` を使わない**（`textContent` と安全な DOM API のみ）。
   - `web/_headers` の CSP（Content-Security-Policy）を緩めない。

---

## 設計とデータフローの要点（詳細は `DESIGN.md`）

- **2 層 DB（SQLite: `data/market.db`, Git 除外）**
  - ETF 構成 DB（`etfs`, `etf_dates`, `etf_holdings`, `etf_changes`）
  - 個別株 DB（`securities`, `quarter_prices`, `financials_annual`, `financials_ttm`, `stock_metrics`）
- **Web 公開物（`dist/`, Git 除外）**
  - `web/` の静的ファイル ＋ `src/export_site.py` が SQLite から生成する分割 JSON（`dist/data/...`）。

---

## 壊しやすい箇所（EDGAR / XBRL / 名寄せ）

- **EDGAR アクセス（`src/edgar.py`）**:
  - 5 req/s 以下でレート制限し、`data/raw/` にキャッシュ。429/5xx は指数バックオフで再試行。
  - `SEC_USER_AGENT` 未設定時は外部アクセスせず明示エラーで止める。
- **N-PORT-P（`src/adapters/nport.py`）**:
  - `assetCat == "EC"` かつ `payoffProfile == "Long"` のみ普通株として抽出（MMF・先物・貸株担保は除外）。
  - 外国籍銘柄は CUSIP が `000000000` のため ISIN を使用。
- **Schwab 旧書類（`src/adapters/schwab_legacy.py`）**:
  - 全銘柄表（"Portfolio Holdings as of …"）のみ採用し、上位50銘柄の Condensed 版を除外。
  - 解析結果の評価額合計が書類の `Total Common Stock` と 1 ドル以内で一致しなければ例外とする検証を外さない。
  - 2019年5月末の SCHD は公開書類がない既知の欠落。
- **ティッカーと銘柄 ID（`src/identify.py`）**:
  - 優先順：`manual_tickers.csv`（CUSIP 指定）→ OpenFIGI（CUSIP/ISIN）→ 旧書類の名寄せ → `manual_tickers.csv`（名前指定）→ SEC `company_tickers.json`。
  - 同一基準日・同一 ETF 内で `security_id` が重複したら例外。
  - 株式クラス違い（例：CWEN / CWEN.A）は CUSIP 指定で区別。
- **SEC XBRL（`src/fundamentals.py` / `src/metrics.py`）**:
  - 10-K の重複年度比較から株式分割係数を推定し、分割跨ぎの 1 株指標（EPS / DPS）を整合させる。
  - 配当性向は同一決算期基準（`DPS ÷ EPS` および総額ベースの `DividendsPaid ÷ NetIncome` / `÷ FCF`）。
  - 基準日株価は保有 ETF の `value_usd ÷ shares` から算出。

---

## 変更後の必須確認

1. `python -m src.check`（または `./run.sh check`）がすべて OK（データ整合性 ＋ 秘密情報・個人情報流出スキャン）。
2. Web UI を触った場合は `python -m src.serve` でローカル配信し、ETF View（列カスタマイズ・基準日切替・行クリック）と Stock View（年次グラフ・年次テーブル・ETF 保有履歴）をブラウザで確認する。
