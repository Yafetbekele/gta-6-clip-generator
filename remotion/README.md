# Clip polish with Remotion

Run `npm ci`, then `npm run dev`. Open the PolishedClip composition.
The default scene is a layout demonstration without source footage.

The Python pipeline supplies source video and Whisper captions as props.
Choose `remotion` in the dashboard Clip renderer setting (restart workers),
or set `editing.renderer = "remotion"` in config.toml with `layout = "fit"`.

`npm run lint` checks TypeScript and React code.
`node render.mjs job.json assets-directory output.mp4` renders a job.
Add `still` after the output path to save a PNG at frame 30 instead.

A job contains `source` (filename inside the assets directory), `streamer`,
`durationSeconds` (up to 60), and `captions` in Remotion Caption JSON format.
The source must already be cut to the exact clip window; caption times are
relative to its start. The Python adapter performs that trim and checks its length.

See the repository README for installation, cleanup, and worker behavior.
