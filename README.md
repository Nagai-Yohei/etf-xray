# etf-xray

100% 公開データ（**SEC EDGAR** + **OpenFIGI**）のみを用いて、米国 ETF の構成銘柄ヒストリーと個別株ファンダメンタルズ・配当指標を閲覧できる静的 Web サイトおよびビルドパイプラインです。

- **UI 言語**: 英語（English）
- **開発ドキュメント・コメント**: 日本語
- **データソース**: SEC EDGAR（N-PORT-P / N-CSR / N-CSRS / N-Q / 10-K / 10-Q XBRL）および OpenFIGI（＋ `config/manual_tickers.csv`）
- **フロントエンド**: 素の HTML / CSS / ES Modules JS ＋ ローカル同梱の Chart.js（Node.js / npm・外部 CDN 不使用）

---

## 1. 環境構築

前提: **Python 3.10+** および **Git**

```bash
# 仮想環境の作成と最小依存（requests, lxml）のインストール
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 流出防止 pre-commit フックの有効化
git config core.hooksPath scripts

# ローカル環境変数ファイルの作成（.gitignore 除外済み）
cp .env.example .env
```

`.env` を開き、`SEC_USER_AGENT` にご自身の識別名と連絡先メールアドレスを設定してください（SEC EDGAR の利用要件）。  
※ `.env` は絶対に Git へコミットしないでください。

---

## 2. ビルド・検査・ローカル確認

```bash
# 全データ取得 → SQLite 構築（data/market.db）→ 静的サイト出力（dist/）
./run.sh build

# キャッシュ済み XBRL のみで高速再ビルド（オフライン）
./run.sh build --skip-etf --offline

# SQLite から dist/（HTML/CSS/JS + 分割 JSON）のみ再出力
./run.sh export

# データ整合性検査 ＋ 秘密情報・個人情報流出スキャン（必須）
./run.sh check

# ローカル確認用サーバー起動（本番と同一の厳格 CSP ヘッダを付与）
./run.sh serve
# → http://127.0.0.1:8787/
```

---

## 3. Cloudflare Pages への公開手順（Direct Upload）

本リポジトリは `data/`（SQLite DB・生キャッシュ）と `.env` を Git 除外しています。  
公開時は、ローカルで **`./run.sh check` がすべて OK** になった **`dist/` フォルダのみ** をアップロードします。

### 方法 A: Cloudflare ダッシュボードからアップロード（最も手軽）

1. ローカルで `./run.sh check` を実行し、`CHECK OK` を確認する。
2. Cloudflare ダッシュボード → **Workers & Pages** → **Create** → **Pages** → **Direct Upload** を選択する。
3. 任意の **Project name**（英小文字・数字・ハイフン、例: `etf-xray`）を入力する。  
   → 自動的に `https://<project-name>.pages.dev` が発行されます。
4. ローカルの **`dist/` フォルダ** をドラッグ＆ドロップして **Deploy site** を押す。
5. （任意）独自ドメインを使う場合は、プロジェクト設定の **Custom domains** から取得済みドメインを追加する。

### 方法 B: Wrangler CLI でデプロイ（2 回目以降の更新に便利）

```bash
./run.sh check
npx wrangler pages deploy dist --project-name <your-project-name>
```

> **テスト公開時に検索エンジンへ掲載したくない場合**  
> `web/robots.txt` に `User-agent: *` と `Disallow: /` を記述して `./run.sh export` → `./run.sh check` 後にデプロイしてください。

---

## 4. ディレクトリ概要

| パス | 役割 |
|---|---|
| `config/` | 対象 ETF 定義（`etfs.json`）・手動ティッカー対応表（`manual_tickers.csv`） |
| `src/` | EDGAR 取得・HTML/XML アダプタ・名寄せ・XBRL 抽出・指標計算・JSON 出力・検証 |
| `web/` | 静的 Web UI（HTML / CSS / ES Modules JS / `_headers` / 同梱 `vendor/chart.umd.min.js`） |
| `scripts/pre-commit` | 秘密情報・個人情報・禁止ファイルのコミット阻止フック |
| `data/` | **[Git 除外]** EDGAR / OpenFIGI キャッシュおよび SQLite DB（`market.db`） |
| `dist/` | **[Git 除外]** 公開用完成ディレクトリ（`web/` + 生成 JSON） |

---

## 5. 免責・出典

本サイトの数値はすべて SEC EDGAR の公開提出書類および OpenFIGI の公開データから機械的に集計しています。  
四半期末株価は保有 ETF の評価額 ÷ 保有株数から算出した推計値です。情報提供のみを目的としており、投資助言ではありません。
