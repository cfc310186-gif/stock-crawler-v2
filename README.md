# 台股分點籌碼雷達儀表板

## 專案介紹

提供今日／排行／個股／我的四個頁面；排行維持覆蓋天數分組顯示。

## 資料來源與期間

資料來源為 TWSE／TPEx 官方行情與富邦分點頁（分點代碼 9A91）。legacy 歷史自 2025-11-03 起，試跑資料截至 2026-11-13。本專案提供資料快照，不是即時更新。

## 資料快照

- 檔名：`data/trial-20261113-v2.5.sqlite`
- 大小：9,560,064 bytes（已對副本 VACUUM，清除已刪除內容殘留；筆數不變）
- publications：249 筆
- branch_flows：22,666 筆

上述筆數是資料列數，不等同交易日數。

## 本地執行

使用 Python 3.12.3，在專案根目錄執行：

```sh
pip install -r requirements.txt
streamlit run m1/dashboard.py --server.address 127.0.0.1
```

公開版不限綁定位址（Streamlit Cloud 綁 0.0.0.0）；本地測試可用 127.0.0.1 或 0.0.0.0。

## Streamlit Community Cloud 部署

- Repo：`cfc310186-gif/stock-crawler-v2`
- 主程式路徑：`m1/dashboard.py`
- 部署時在 Advanced settings 明確選擇 Python 3.12。

`runtime.txt` 僅記錄版本意圖、供參考，Streamlit Community Cloud 可能忽略它。部署後須核對建置日誌，確認實際 Python 與套件版本，並再驗證 App 行為。

## 公開資料與隱私界線

此公開儀表板不含持倉資料，也不提供 LINE 傳送功能。通知區僅顯示 notification_outbox 的狀態與筆數統計；目前快照只有 3 筆 draft，沒有收件者、沒有實際傳送紀錄，不展示草稿內容。

整份 SQLite 會隨 repo 公開，任何人可下載；畫面不顯示不等於沒有公開。

## 共用 Watchlist 與保存限制

公開站的 Watchlist 為所有訪客共用，並非個人帳戶隔離，請勿輸入私人資訊。編輯僅保存於執行環境，不會回寫 GitHub；環境重建、重新部署或平台回收時可能遺失，隨時可能回到 repo 內的資料快照。

## 資料限制與使用注意事項

僅涵蓋當期可取得的 Top-50 買超＋Top-50 賣超範圍，非全分點排行，未上榜日不計加總。估算張數不是實際張數；成本無法驗證，不提供。

## 已知限制

localhost 檢查已在公開版移除（私人版 `m1/dashboard.py` 仍保留，請勿把公開版檔案複製回私人環境）。本次 staging 尚未在 Streamlit Community Cloud 上實測通過，部署後須再驗證。
