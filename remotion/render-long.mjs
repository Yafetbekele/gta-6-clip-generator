// Renders a long-form composition: node render-long.mjs props.json assets-dir output LongForm|Thumbnail
import {readFile, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir, cpus} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {bundle} from '@remotion/bundler';
import {selectComposition, renderMedia, renderStill} from '@remotion/renderer';

const [job, assets, output, id = 'LongForm'] = process.argv.slice(2);
if (!job || !assets || !output) throw new Error('Usage: node render-long.mjs props.json assets-dir output [LongForm|Thumbnail]');
const inputProps = JSON.parse(await readFile(job, 'utf8'));
const root = path.dirname(fileURLToPath(import.meta.url));
const bundleDir = await mkdtemp(path.join(tmpdir(), 'gta-long-'));
try {
  const serveUrl = await bundle({entryPoint: path.join(root, 'src/index.ts'), publicDir: path.resolve(assets), outDir: bundleDir});
  const stills = id === 'Stills';  // STILL_FRAMES=100,200 renders LongForm frames to <output>/frame_N.png
  const composition = await selectComposition({serveUrl, id: stills ? 'LongForm' : id, inputProps, timeoutInMilliseconds: 180000});
  const base = {serveUrl, composition, inputProps, timeoutInMilliseconds: 180000};
  if (stills) for (const fr of (process.env.STILL_FRAMES ?? '0').split(',').map(Number))
    await renderStill({...base, output: path.resolve(output, `frame_${fr}.png`), frame: fr, imageFormat: 'png'});
  else if (id === 'Thumbnail') await renderStill({...base, output: path.resolve(output), frame: 0, imageFormat: 'png'});
  else {
    let last = -1;
    await renderMedia({...base, outputLocation: path.resolve(output), codec: 'h264', audioCodec: 'aac',
      audioBitrate: '192k', pixelFormat: 'yuv420p', crf: 18, x264Preset: 'medium',
      concurrency: Math.max(2, Math.min(6, cpus().length - 2)),
      onProgress: ({progress}) => {
        const pct = Math.floor(progress * 100);
        if (pct !== last && pct % 5 === 0) { last = pct; console.log(`render ${pct}%`); }
      },
      ...(process.env.REMOTION_LICENSE_KEY ? {licenseKey: process.env.REMOTION_LICENSE_KEY} : {})});
  }
} finally {
  await rm(bundleDir, {recursive: true, force: true});
}
