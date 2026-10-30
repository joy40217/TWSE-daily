// Cloudflare Worker：準時觸發「每日抓取證交所資料」workflow
//
// GitHub 自己的排程常延遲 4～5 小時，所以改由 Cloudflare 的 Cron Trigger 準時呼叫
// GitHub API 的 workflow_dispatch（跟在 Actions 分頁手動按 Run workflow 一樣，幾秒內就會開始跑）。
// 設定步驟見 README「準時觸發（Cloudflare Workers）」。
//
// 需要的 Secret：GITHUB_TOKEN（fine-grained token，只開放這個 repo、權限 Actions: Read and write）
// Cron Triggers（UTC，台灣 = UTC+8）：
//   7 7 * * 1-5     台灣 15:07  收盤行情
//   23 10 * * 1-5   台灣 18:23  本益比、三大法人
//   23 14 * * 1-5   台灣 22:23  融資融券、送 Discord 通知

const REPO = "joy40217/TWSE-daily";
const WORKFLOW = "daily.yml";

async function dispatch(env) {
  const res = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "twse-daily-dispatch",   // GitHub API 規定要有 User-Agent
    },
    body: JSON.stringify({ ref: "main", inputs: { report: true } }),
  });
  if (res.status !== 204) {
    // 丟出錯誤，Cloudflare 的記錄才會標成失敗
    throw new Error(`GitHub 回應 ${res.status}：${await res.text()}`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    try {
      await dispatch(env);
    } catch (e) {
      // GitHub 偶爾短暫出錯，等 1 分鐘再試一次
      console.log(`第 1 次失敗，1 分鐘後重試：${e.message}`);
      await new Promise((r) => setTimeout(r, 60_000));
      await dispatch(env);
    }
    console.log(`已觸發 ${WORKFLOW}（cron ${event.cron}）`);
  },

  // 網址被打開時什麼都不做，避免任何人都能觸發 workflow
  async fetch() {
    return new Response("twse-daily dispatch worker", { status: 200 });
  },
};
