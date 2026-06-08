// Render the bmc-agent animation to a real video file.
//
//   npm i -D playwright && npx playwright install chromium
//   node presentation/record-demo.mjs
//
// Produces presentation/bmc-agent-demo.webm (720p, one full loop).
// To get an .mp4 (for PowerPoint/Keynote):
//   ffmpeg -i presentation/bmc-agent-demo.webm -c:v libx264 -pix_fmt yuv420p \
//          -movflags +faststart presentation/bmc-agent-demo.mp4
import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const here = dirname(fileURLToPath(import.meta.url));
const page = join(here, 'bmc-agent-onepager.html');
const W = 1280, H = 720, MS = 38000;   // ~one full narrated loop

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: W, height: H },
  recordVideo: { dir: here, size: { width: W, height: H } },
});
const p = await ctx.newPage();
await p.goto('file://' + page + '?embed=1');
await p.waitForTimeout(MS);
const video = p.video();
await ctx.close();                       // finalizes the recording
await browser.close();
console.log('saved:', await video.path());
