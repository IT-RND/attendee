# Boga (Meeting) Assistant-managed Zoom RTMS

Zoom [Realtime Media Streams (RTMS)](https://developers.zoom.us/docs/rtms/) is a Zoom-native data pipeline that gives your app access to live audio, video, transcript, and screenshare data from Zoom meetings. Unlike meeting bots which join as visible participants, RTMS streams meeting data directly to your application without adding anyone to the call.

Boga (Meeting) Assistant provides support for RTMS through a new API entity called **App Sessions**. When a user activates an RTMS app during a meeting, Zoom sends a webhook to your application, and you forward that payload to Boga (Meeting) Assistant to create an app session. Boga (Meeting) Assistant then handles connecting to the RTMS stream, processing the media, and delivering transcripts and recordings.

For reference implementations, see the example programs for building a [notetaker](https://github.com/attendee-labs/rtms-notetaker-example) and [sales coach](https://github.com/attendee-labs/rtms-sales-coach-example) with Boga (Meeting) Assistant and RTMS. For quick demo of an RTMS app built with Boga (Meeting) Assistant, see the video [here](https://www.youtube.com/watch?v=56DzvzJHSv4).

## RTMS vs Bots

There are two key differences between RTMS and bots:

**No bot participant in the meeting.** RTMS does not add a participant to the call. The meeting data is streamed to your app through Zoom's infrastructure, so there is no "bot has joined" notification and no extra attendee in the participant list. See [here](https://youtu.be/YKeVFXSFRGg?si=Vgkl50hOnz4VlnQi&t=149) for a video showing how RTMS apps appear within the Zoom client:

**The user controls when your app connects to the meeting.** With a bot, you are in control of when the bot attempts to join the meeting — you make an API call and Boga (Meeting) Assistant sends the bot in. With RTMS, the user is in control. When the user opens your RTMS app, Zoom sends your app a webhook that it must respond to. The user can also pause the RTMS app's recording at any time.

Other advantages of RTMS:

- **No OBF token required.** RTMS is not affected by Zoom's [March 2, 2026 deadline](https://developers.zoom.us/blog/transition-to-obf-token-meetingsdk-apps/) requiring OBF tokens for Meeting SDK bots joining external meetings. You also do not need to implement join tokens or any OAuth flow logic in your app.
- **Less CPU usage.** RTMS sends encoded video frames, which is less CPU-intensive to process than the raw video frames sent when using the Zoom Meeting SDK.

Limitations of RTMS:

- **Receive data only.** RTMS cannot send data back into the meeting. If you need your app to post messages to the meeting chat, or send video and audio into the meeting, you'll need a bot.

## How to implement RTMS with Boga (Meeting) Assistant

Official references: [Zoom RTMS — add features](https://developers.zoom.us/docs/rtms/meetings/add-features/), [Attendee guide — Zoom RTMS (App Sessions)](https://mintlify.wiki/attendee-labs/attendee/guides/zoom-rtms).

### Create an RTMS App in the Zoom Developer Portal

1. Go to the [Zoom Developer Portal](https://marketplace.zoom.us/user/build) and create a new General app.

2. On the sidebar select 'Basic Information'.
3. For the OAuth redirect URLs, you can write https://zoom.us or any other URL, assuming your app does not need to use OAuth.

4. On the sidebar select 'Access'.
5. Click 'Add new Event Subscription'.
6. Subscribe to **`meeting.rtms_started`** and **`meeting.rtms_stopped`** (shown in the portal as RTMS started / RTMS stopped).
7. Set the 'Event notification endpoint URL' to an **HTTPS URL on your own application** that will verify Zoom signatures and forward RTMS data to Boga (Meeting) Assistant. Do not point this URL directly at Boga (Meeting) Assistant — your server must translate the Zoom webhook into `POST /api/v1/app_sessions`.
8. Save the changes.

9. On the sidebar select 'Scopes'.
10. Add the following scopes:
    - meeting:read:meeting_audio
    - meeting:read:meeting_transcript
    - meeting:read:meeting_chat
    - meeting:read:meeting_video
    - meeting:read:meeting_screenshare

11. On the sidebar select 'Local test'.
12. Click the 'Add app now' button and authorize the app.

13. Go to your Zoom App Settings at https://zoom.us/profile/setting?tab=zoomapps
14. Enable share realtime meeting content with apps
15. Under "Auto-start apps that access shared realtime meeting content" click the "Choose an app to auto-start" button and select your app.

### Register your RTMS App with Boga (Meeting) Assistant

1. Go to the Boga (Meeting) Assistant dashboard and create a new project for your RTMS app.
2. Navigate to **Settings → Credentials**.
3. Under **Zoom OAuth App Credentials**, click **Add OAuth App**.
4. Enter the **Client ID** and **Client Secret** from the same Zoom General app you use for RTMS (this lets Boga connect to the RTMS stream on your behalf).
5. Click **Save**.

### Configure webhooks in Boga (Meeting) Assistant

1. Go to **Settings → Webhooks**.
2. Click **Create Webhook** and select the triggers you need from Boga (Meeting) Assistant. For app sessions, subscribe to **`bot.state_change`** — it fires when the RTMS-backed session changes state (e.g. moves to `ended`), despite the `bot_` naming.
3. Use a **different** HTTPS URL than your Zoom event subscription URL; these callbacks originate from Boga (Meeting) Assistant, not Zoom.
4. Click **Create** to save your webhook.

### Option A — Zoom Event Subscription URL on Boga (no separate forwarder)

If your Zoom RTMS app is registered under the same project (**Settings → Credentials → Zoom OAuth App**), you can point Zoom’s **Event notification endpoint URL** at Boga’s Zoom webhook for that app:

`https://<your-site-domain>/external_webhooks/zoom/oauth_apps/<ZoomOAuthApp.object_id>`

Replace `<your-site-domain>` with your deployment host (for example `meeting-assistant.boga.co.id`) and `<ZoomOAuthApp.object_id>` with the **object ID** of the Zoom OAuth app row in that project (the same ID used in the dashboard URL or API for that credential).

Requirements:

- The **Webhook Secret Token** configured in the Zoom Developer Portal for that event subscription must match the **webhook secret** stored on that Zoom OAuth app in Boga (used for signature verification).
- Boga will create an app session on **`meeting.rtms_started`** and request disconnect on **`meeting.rtms_stopped`**, using default transcription/recording behavior (same defaults as **`POST /api/v1/app_sessions`** without extra fields). For custom `metadata`, `transcription_settings`, or per-request `webhooks`, use Option B and call the API yourself.

### Option B — Your own server forwards to the App Sessions API

Handle `meeting.rtms_started` from Zoom and call **`POST /api/v1/app_sessions`** with:

- Header: `Authorization: Token <YOUR_BOGA_API_KEY>`
- JSON body: at minimum `{ "zoom_rtms": { ... } }` where `zoom_rtms` contains the RTMS fields Zoom sends (`meeting_uuid`, `rtms_stream_id`, `server_urls`, optional `operator_id`). You may forward either the object under Zoom’s `payload` or the full webhook object — the API normalizes common Zoom field names (`meetingUuid`, `rtmsStreamId`, `serverUrls`).

Optional fields are the same as for bots: `metadata`, `transcription_settings`, `recording_settings`, `webhooks`, etc.

See the runnable example in this repository at [`examples/zoom_rtms_node/README.md`](../examples/zoom_rtms_node/README.md), and the upstream [notetaker sample](https://github.com/attendee-labs/rtms-notetaker-example/blob/d51d7f79d13151ffa97369bf264736f244fe35e4/index.js#L70).

### Add code to your application to handle the bot.state_change webhook from Boga (Meeting) Assistant

Subscribe to **`bot.state_change`**. When `new_state` is **`ended`**, fetch transcript and media via:

- `GET /api/v1/app_sessions/{id}/transcript`
- `GET /api/v1/app_sessions/{id}/media`
- `GET /api/v1/app_sessions/{id}/participant_events`

The webhook body identifies the app session (see payload fields in your Boga dashboard webhook docs). Example handler logic: [notetaker sample](https://github.com/attendee-labs/rtms-notetaker-example/blob/d51d7f79d13151ffa97369bf264736f244fe35e4/index.js#L119).

### Ending a session from Zoom (optional)

On **`meeting.rtms_stopped`**, you may call **`POST /api/v1/app_sessions/end`** with body `{ "zoom_rtms": { "rtms_stream_id": "<same id as when started>" } }` to request disconnect.

## Other App session API endpoints

**`GET /api/v1/app_sessions/{id}/media`**

Returns the recording and media files for a completed app session. Only available after the session has moved to the `ended` state.

### Get App Session Transcript

**`GET /api/v1/app_sessions/{id}/transcript`**

Returns the full transcript for a completed app session.

### Get App Session Participant Events

**`GET /api/v1/app_sessions/{id}/participant_events`**

Returns participant join/leave events for the app session.

## FAQ

### Does RTMS require the On Behalf Of (OBF) token?

No. RTMS is a separate integration path from the Meeting SDK and is not affected by Zoom's [March 2, 2026 OBF token deadline](https://developers.zoom.us/blog/transition-to-obf-token-meetingsdk-apps/). If you switch your Zoom integration from bots to RTMS, you do not need to implement the On Behalf Of (OBF) token or any of the other Zoom tokens used in the Meeting SDK.

### What happens if the host doesn't have RTMS enabled?

Your app will not receive the `meeting.rtms_started` webhook and no data will be captured. RTMS requires the meeting host's Zoom account to have the feature enabled and your app authorized. This is a key consideration if your users join meetings hosted by people outside your organization.