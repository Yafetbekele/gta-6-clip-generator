import {readFile, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {bundle} from '@remotion/bundler';
import {selectComposition, renderMedia, renderStill} from '@remotion/renderer';

const [job, assets, output, mode] = process.argv.slice(2);
if (!job || !assets || !output) throw new Error('Usage: node render.mjs job.json assets-dir output.mp4 [still]');
const inputProps = JSON.parse(await readFile(job, 'utf8'));
const root = path.dirname(fileURLToPath(import.meta.url));
const bundleDir = await mkdtemp(path.join(tmpdir(), 'gta-remotion-'));
try {
  const serveUrl = await bundle({entryPoint: path.join(root, 'src/index.ts'),
    publicDir: path.resolve(assets), outDir: bundleDir});
  const composition = await selectComposition({serveUrl, id: 'PolishedClip', inputProps});
  const options = {serveUrl, composition, inputProps, outputLocation: path.resolve(output)};
  if (mode === 'still') await renderStill({...options, output: path.resolve(output), frame: 30});
  else await renderMedia({...options, codec: 'h264', audioCodec: 'aac', pixelFormat: 'yuv420p',
    crf: 20, concurrency: 2, ...(process.env.REMOTION_LICENSE_KEY ? {licenseKey: process.env.REMOTION_LICENSE_KEY} : {})});
} finally {
  await rm(bundleDir, {recursive: true, force: true});
}
