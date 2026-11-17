// Prompt Spider — pm2 process definitions.
//
// Paths resolve from the environment so this file is portable:
//   PROMPT_SPIDER_PYTHON   full path to python.exe (defaults to `python` on PATH)
//   PROMPT_SPIDER_HOME     where prompts.db lives (defaults to ~/.prompt-spider)
//   PROMPT_SPIDER_PROFILE  persistent Edge profile directory
//   PROMPT_SPIDER_PORT     web UI port (default 8420)
//
// Set PROMPT_SPIDER_PYTHON explicitly. `python` on PATH is not necessarily the
// interpreter that has playwright installed, and a scheduled job that dies on
// import is silent — which is the failure mode this whole project exists to
// avoid.
//
// SCHEDULING
// ----------
// The two halves of the harvest have very different costs, so they run on
// different clocks:
//
//   local    Reads Scout / Copilot CLI / VS Code straight off disk. No browser,
//            no lock, finishes in seconds. Safe to run hourly, all day.
//
//   browser  Opens a VISIBLE Edge window (headless is rejected on the SSO
//            profile) and takes the shared profile lock. Restricted to
//            18:00-06:00 by choice.
//
// Minutes are staggered so the jobs never contend for the database or the lock:
//   :00    browser incremental   (18:00-06:00 only, 45min budget)
//   :15    local                 (hourly, all day)
//   03:40  backfill              (nightly, between the 03:00 and 04:00 runs)
//   23:50  discover              (nightly while catching up; see note below)
//
// The 18:00-06:00 window is written as an explicit hour list, not as
// "18-23,0-6". pm2 7.0.1 cannot parse a comma-separated list of RANGES in the
// hour field: it prints "[PM2] cron restart at ..." as if accepted, then exits
// 1 with nothing on stderr and starts NOTHING — including the apps that parsed
// fine. A single range ("18-23") and an explicit list ("0,1,2,18,19") are both
// accepted; only the combination fails. Verified against pm2 7.0.1.
//
// Budgets sit below the gap to the next run, so a job finishes before its
// successor is due. If one does overrun, pm2's cron_restart kills it — the
// harvester commits per item, so at most one conversation is lost and the next
// run resumes cleanly.
//
// cron_restart requires autorestart:false. Without it pm2 reads each clean exit
// as a crash and relaunches immediately, turning an hourly job into a
// continuous loop that would hold the Edge lock permanently.

const PY = process.env.PROMPT_SPIDER_PYTHON || "python";
// The web UI wants no console window; the harvest jobs want one, so pm2 can
// capture their output into the log files.
const PYW = PY.replace(/python\.exe$/i, "pythonw.exe");

const env = {
  PROMPT_SPIDER_HOME: process.env.PROMPT_SPIDER_HOME || "",
  PROMPT_SPIDER_PROFILE: process.env.PROMPT_SPIDER_PROFILE || "",
  PROMPT_SPIDER_PYTHON: PY,
};

const job = (name, args, cron) => ({
  name,
  script: PY,
  args,
  interpreter: "none",
  cwd: __dirname,
  env,
  autorestart: false,      // one-shot: cron_restart is what relaunches it
  cron_restart: cron,
  time: true,              // timestamped logs, so runs can be told apart
});

module.exports = {
  apps: [
    // Always-on search UI.
    //
    // kill_timeout is raised because uvicorn needs a moment to release the
    // socket. pm2's 1.6s default expires first on Windows, so the old process
    // survives the restart, keeps port 8420, and every subsequent attempt dies
    // with EADDRINUSE while pm2 reports "online" — a restart loop that leaves
    // an orphan serving stale code.
    {
      name: "prompt-spider",
      script: PYW,
      args: "server.py",
      interpreter: "none",
      cwd: __dirname,
      env: { ...env, PORT: process.env.PROMPT_SPIDER_PORT || "8420" },
      autorestart: true,
      max_restarts: 10,
      kill_timeout: 8000,
    },

    // Local files only. Cheap and invisible, so it runs all day. These sources
    // are read from disk and lose nothing by waiting, but there is no reason to
    // make them wait.
    job("prompt-spider-local", "harvest.py --mode local", "15 * * * *"),

    // Browser harvest. `incremental` walks the top of the recency-ordered
    // sidebar for anything new, then extracts pending items oldest-first, so a
    // single mode both keeps up and drains the backlog.
    //
    // This is the half that matters for data loss: M365 and Cowork are a
    // snapshot, not an archive, and a conversation that ages out of the sidebar
    // before it is collected is gone for good.
    //
    // Hours listed individually — see the pm2 cron note at the top of the file.
    job("prompt-spider-web",
        "harvest.py --mode incremental --budget-min 45 --limit 150",
        "0 0,1,2,3,4,5,6,18,19,20,21,22,23 * * *"),

    // Backfill. Re-visits conversations already stored whose rows predate the
    // hydration-payload path, filling in ts_source, response_excerpt, title,
    // message_id and author in place.
    //
    // Finite work: it only queues conversations that would actually gain
    // something, so it drains and then costs nothing. Safe to remove once the
    // queue reaches zero — check with `py align_check.py`.
    job("prompt-spider-backfill",
        "harvest.py --mode backfill --budget-min 25 --limit 120",
        "40 3 * * *"),

    // Full sidebar walk. Enumerates only — it records conversations as pending
    // and extracts nothing, so the hourly `web` job drains whatever it finds
    // over the following nights.
    //
    // This is the deep catch-up: the sidebar holds 6,000+ conversations and a
    // shallow incremental walk only ever sees the top of it.
    //
    // 23:50 is deliberate. It is after the 23:00 web run can possibly still be
    // holding the Edge lock (45-minute budget, so finished by 23:45), and the
    // 360-minute budget lands it before the 06:00 run. The hourly runs in
    // between will find the lock held and skip cleanly — that is the intended
    // behaviour on a walk night, not a fault.
    //
    // Nightly WHILE CATCHING UP. Once `pending` stops falling the corpus is
    // complete, and this should drop to weekly (`50 23 * * 0`) — a nightly
    // six-hour walk is pure waste once there is nothing new to find.
    job("prompt-spider-discover",
        "harvest.py --mode discover --budget-min 360",
        "50 23 * * *"),
  ],
};
