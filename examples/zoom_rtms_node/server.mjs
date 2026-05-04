/**
 * Reference Zoom RTMS integration for Attendee App Sessions.
 *
 * Endpoints:
 *   POST /zoom/webhook     — Zoom Event Subscription (RTMS started/stopped)
 *   POST /attendee/webhook — Attendee outbound webhook (e.g. bot.state_change)
 *
 * Environment:
 *   ATTENDEE_BASE_URL  — default https://app.attendee.dev
 *   ATTENDEE_API_KEY   — required
 *   PORT               — default 3000
 *
 * Verify Zoom signatures with ZOOM_WEBHOOK_SECRET before processing (not implemented here).
 */

import http from "node:http";

const ATTENDEE_BASE_URL = process.env.ATTENDEE_BASE_URL || "https://app.attendee.dev";
const ATTENDEE_API_KEY = process.env.ATTENDEE_API_KEY;
const PORT = Number(process.env.PORT) || 3000;

function json(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(data),
  });
  res.end(data);
}

async function readJson(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return { parseError: true, raw };
  }
}

async function createAppSession(zoomRtmsBody, extra = {}) {
  if (!ATTENDEE_API_KEY) {
    throw new Error("ATTENDEE_API_KEY is not set");
  }
  const url = `${ATTENDEE_BASE_URL.replace(/\/$/, "")}/api/v1/app_sessions`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Token ${ATTENDEE_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      zoom_rtms: zoomRtmsBody,
      metadata: extra.metadata || {},
      transcription_settings: extra.transcription_settings || {
        deepgram: { language: "en-US" },
      },
    }),
  });
  const text = await res.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    throw new Error(`createAppSession failed: ${res.status} ${text}`);
  }
  return data;
}

async function endAppSession(rtmsStreamId) {
  if (!ATTENDEE_API_KEY) {
    throw new Error("ATTENDEE_API_KEY is not set");
  }
  const url = `${ATTENDEE_BASE_URL.replace(/\/$/, "")}/api/v1/app_sessions/end`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Token ${ATTENDEE_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      zoom_rtms: { rtms_stream_id: rtmsStreamId },
    }),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`endAppSession failed: ${res.status} ${text}`);
  }
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

async function fetchTranscript(appSessionId) {
  const url = `${ATTENDEE_BASE_URL.replace(/\/$/, "")}/api/v1/app_sessions/${appSessionId}/transcript`;
  const res = await fetch(url, {
    headers: { Authorization: `Token ${ATTENDEE_API_KEY}` },
  });
  return res.json();
}

async function fetchMedia(appSessionId) {
  const url = `${ATTENDEE_BASE_URL.replace(/\/$/, "")}/api/v1/app_sessions/${appSessionId}/media`;
  const res = await fetch(url, {
    headers: { Authorization: `Token ${ATTENDEE_API_KEY}` },
  });
  return res.json();
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host}`);

  if (req.method === "POST" && url.pathname === "/zoom/webhook") {
    const body = await readJson(req);
    if (!body || body.parseError) {
      return json(res, 400, { error: "Invalid JSON" });
    }

    // Implement Zoom endpoint.url_validation (challenge) per current Zoom Event Subscription docs.

    const event = body.event;
    try {
      if (event === "meeting.rtms_started") {
        // Forward Zoom payload; Attendee API normalizes camelCase / nested payload.
        const created = await createAppSession(body.payload || body);
        console.log("App session created:", created.id || created);
        return json(res, 200, { ok: true });
      }
      if (event === "meeting.rtms_stopped") {
        const p = body.payload || body;
        const streamId = p.rtms_stream_id || p.rtmsStreamId;
        if (streamId) {
          await endAppSession(streamId);
        }
        return json(res, 200, { ok: true });
      }
    } catch (e) {
      console.error(e);
      return json(res, 500, { error: String(e.message || e) });
    }

    return json(res, 200, { ok: true, ignored: event });
  }

  if (req.method === "POST" && url.pathname === "/attendee/webhook") {
    const body = await readJson(req);
    if (!body || body.parseError) {
      return json(res, 400, { error: "Invalid JSON" });
    }

    const trigger = body.trigger;
    const data = body.data || {};
    if (trigger === "bot.state_change" && data.new_state === "ended") {
      // App sessions expose app_session_id in the webhook envelope (not bot_id).
      const appSessionId = body.app_session_id || data.bot_id || data.object_id;
      if (appSessionId && ATTENDEE_API_KEY) {
        try {
          const transcript = await fetchTranscript(appSessionId);
          const media = await fetchMedia(appSessionId);
          console.log("Session ended; transcript keys:", Object.keys(transcript || {}));
          console.log("Session ended; media:", media);
        } catch (e) {
          console.error("Fetch after ended failed:", e);
        }
      }
    }
    return json(res, 200, { ok: true });
  }

  return json(res, 404, { error: "Not found" });
});

server.listen(PORT, () => {
  console.log(`Listening on http://localhost:${PORT}`);
  console.log(`  Zoom webhooks: POST /zoom/webhook`);
  console.log(`  Attendee webhooks: POST /attendee/webhook`);
});
