# Zoom RTMS → Attendee (App Sessions) example

Minimal Node.js **reference** for:

1. Receiving Zoom Event Subscription webhooks (`meeting.rtms_started` / `meeting.rtms_stopped`).
2. Creating and ending **App Sessions** on Attendee via `POST /api/v1/app_sessions` and `POST /api/v1/app_sessions/end`.
3. Receiving Attendee outbound webhooks (`bot.state_change`) and fetching transcript/media after `ended`.

## Prerequisites

- Node.js 18+ (uses global `fetch`).
- A Zoom **General** app with RTMS events and scopes (see [docs/zoom_rtms.md](../../docs/zoom_rtms.md)).
- An Attendee project **API key** and the same Zoom app registered under **Settings → Credentials → Zoom OAuth App**.

## Configuration

| Variable | Description |
| -------- | ----------- |
| `PORT` | HTTP port (default `3000`) |
| `ATTENDEE_BASE_URL` | e.g. `https://app.attendee.dev` |
| `ATTENDEE_API_KEY` | Project API key (`Authorization: Token …`) |
| `ZOOM_WEBHOOK_SECRET` | From Zoom app **Feature** → **Event subscription** → **Secret Token** (verify `x-zm-signature` in production) |

## Run

```bash
cd examples/zoom_rtms_node
export ATTENDEE_API_KEY=your_key
export ATTENDEE_BASE_URL=https://app.attendee.dev
node server.mjs
```

Point Zoom’s **Event notification endpoint** to `https://your-host/zoom/webhook` (use ngrok for local dev).

In Attendee **Settings → Webhooks**, set your **Attendee callback** URL to `https://your-host/attendee/webhook` and subscribe to **`bot.state_change`**.

## Security

`server.mjs` only parses JSON; **you must verify** Zoom webhook signatures using `ZOOM_WEBHOOK_SECRET` before trusting `req.body`. See [Zoom webhook security](https://developers.zoom.us/docs/api/rest/webhook-reference/#verify-webhook-events).

Attendee-to-your-app webhook verification depends on how your project configured outbound webhooks (check Attendee dashboard docs for signing headers).
