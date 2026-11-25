import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

// Leonida is Rockstar's Florida. This map is drawn from Florida's real coastline (rough outline, our own
// drawing) and pins sit on the real places each region is based on.
export type MapPoint = {name: string; place: string; lon: number; lat: number; start: number};

const PINK = '#ff4fa3';
const CYAN = '#38e8ff';
export const W = 940;
export const H = 930;
export const px = (lon: number) => (lon + 88) * 110 + 20;
export const py = (lat: number) => (31.4 - lat) * 126 + 20;

const COAST: [number, number][] = [
  [-87.6, 30.99], [-85.0, 31.0], [-84.86, 30.71], [-82.21, 30.57], [-82.0, 30.75],
  [-81.45, 30.71], [-81.4, 30.3], [-81.26, 29.8], [-81.05, 29.3], [-80.85, 28.95], [-80.6, 28.6],
  [-80.55, 28.4], [-80.62, 28.2], [-80.45, 27.8], [-80.3, 27.4], [-80.1, 27.0], [-80.03, 26.6],
  [-80.08, 26.1], [-80.13, 25.75], [-80.3, 25.4], [-80.4, 25.2], [-80.7, 25.15], [-81.05, 25.12],
  [-81.15, 25.35], [-81.35, 25.8], [-81.75, 25.98], [-81.85, 26.4], [-82.05, 26.55], [-82.15, 26.75],
  [-82.4, 27.1], [-82.6, 27.4], [-82.75, 27.75], [-82.65, 28.0], [-82.85, 28.2], [-82.7, 28.7],
  [-82.75, 29.0], [-83.05, 29.15], [-83.4, 29.55], [-83.7, 29.95], [-84.1, 30.08], [-84.35, 30.0],
  [-84.7, 29.8], [-85.05, 29.68], [-85.35, 29.68], [-85.4, 29.95], [-85.7, 30.12], [-86.2, 30.38],
  [-86.8, 30.4], [-87.3, 30.32], [-87.45, 30.3], [-87.4, 30.45], [-87.6, 30.7],
];
export const KEYS: [number, number, number][] = [
  [-80.42, 25.12, 9], [-80.58, 24.98, 10], [-80.8, 24.86, 9], [-81.05, 24.74, 11], [-81.3, 24.67, 9],
  [-81.52, 24.63, 8], [-81.78, 24.56, 10],
];
export const LAND = 'M' + COAST.map(([lo, la]) => `${px(lo).toFixed(1)},${py(la).toFixed(1)}`).join(' L') + ' Z';

const Pin = ({x, y, s, pulse}: {x: number; y: number; s: number; pulse: number}) => <g>
  <circle cx={x} cy={y} r={18 + 50 * pulse} fill="none" stroke={PINK} strokeWidth={4 * s} opacity={1 - pulse}/>
  <circle cx={x} cy={y} r={16 * s} fill={PINK} stroke="white" strokeWidth={5 * s}/>
</g>;

/** Florida/Leonida map with the trail of visited points and the current pin moving in. `t` = frames since
 *  the current point started. `big` adds the place label next to the pin. */
export const LeonidaMap = ({points, idx, t, big}: {points: MapPoint[]; idx: number; t: number; big?: boolean}) => {
  const {fps} = useVideoConfig();
  const frame = useCurrentFrame();
  const cur = points[idx];
  const prev = points[idx - 1] ?? cur;
  const k = spring({frame: t, fps, config: {damping: 200}, durationInFrames: Math.round(fps * 1.1)});
  const x = interpolate(k, [0, 1], [px(prev.lon), px(cur.lon)]);
  const y = interpolate(k, [0, 1], [py(prev.lat), py(cur.lat)]);
  const pulse = (frame % 45) / 45;
  const trail = points.slice(0, idx + 1);
  return <svg viewBox={`0 0 ${W} ${H}`} style={{width: '100%', height: '100%'}}>
    <defs>
      <linearGradient id="land" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stopColor="#2a2f4a"/><stop offset="1" stopColor="#1a1d30"/>
      </linearGradient>
      <filter id="glow"><feGaussianBlur stdDeviation="6"/></filter>
      <pattern id="grid" width="60" height="60" patternUnits="userSpaceOnUse">
        <path d="M60 0 L0 0 0 60" fill="none" stroke="rgba(56,232,255,.07)" strokeWidth="2"/>
      </pattern>
    </defs>
    <rect width={W} height={H} fill="url(#grid)"/>
    <path d={LAND} fill="none" stroke={CYAN} strokeWidth={10} opacity={0.35} filter="url(#glow)"/>
    <path d={LAND} fill="url(#land)" stroke={CYAN} strokeWidth={3} strokeLinejoin="round"/>
    {KEYS.map(([lo, la, r], i) => <ellipse key={i} cx={px(lo)} cy={py(la)} rx={r} ry={r * 0.55}
      fill="url(#land)" stroke={CYAN} strokeWidth={2.5}/>)}
    <ellipse cx={px(-80.83)} cy={py(26.95)} rx={22} ry={25} fill="#0b1626" stroke={CYAN} strokeWidth={2}/>
    {trail.length > 1 ? <polyline points={trail.slice(0, -1).map((p) => `${px(p.lon)},${py(p.lat)}`).join(' ') + ` ${x},${y}`}
      fill="none" stroke="white" strokeWidth={3.5} strokeDasharray="10 10" opacity={0.6}/> : null}
    {trail.slice(0, -1).map((p, i) => <circle key={i} cx={px(p.lon)} cy={py(p.lat)} r={9} fill={CYAN}
      stroke="#0b0b18" strokeWidth={3}/>)}
    <Pin x={x} y={y} s={big ? 1.25 : 1.6} pulse={pulse}/>
    {big ? (() => {
      const left = x > W * 0.55;
      const o = interpolate(t, [fps * 0.8, fps * 1.2], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
      return <g opacity={o} transform={`translate(${left ? x - 34 : x + 34}, ${Math.max(60, y - 14)})`}>
        <text textAnchor={left ? 'end' : 'start'} fontFamily="Inter" fontWeight={800} fontSize={34} fill="white"
          stroke="#0b0b18" strokeWidth={8} paintOrder="stroke">{cur.place}</text>
      </g>;
    })() : null}
  </svg>;
};
