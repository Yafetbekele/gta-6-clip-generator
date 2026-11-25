import {AbsoluteFill, Img, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig, Easing} from 'remotion';
import {H, KEYS, LAND, W, px, py, type MapPoint} from './LeonidaMap';

const PINK = '#ff4fa3';
const CYAN = '#38e8ff';
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

export type Compare = {game: string; real: string; name: string; place: string; start: number; dur: number; focus?: number};
export type Intro = {start: number; dur: number; title: string; sub: string};

/** Game shot and real photo side by side, sliding in from both edges. */
export const CompareCard = ({c, head, body}: {c: Compare; head: string; body: string}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const len = c.dur * fps;
  const k = spring({frame: f, fps, config: {damping: 22, stiffness: 140}});
  const out = interpolate(f, [len - 8, len], [1, 0], clamp);
  const zoom = interpolate(f, [0, len], [1.04, 1.12], clamp);
  const label = (text: string, color: string, sub: string, right: boolean) =>
    <div style={{position: 'absolute', top: 40, [right ? 'right' : 'left']: 40, textAlign: right ? 'right' : 'left',
      opacity: interpolate(f, [8, 16], [0, 1], clamp)}}>
      <div style={{display: 'inline-block', background: color, color: '#0b0b18', fontFamily: body, fontWeight: 800,
        fontSize: 30, letterSpacing: 4, padding: '6px 16px', borderRadius: 6}}>{text}</div>
      <div style={{fontFamily: head, fontSize: 54, color: 'white', marginTop: 8,
        textShadow: '0 4px 20px rgba(0,0,0,.9)'}}>{sub.toUpperCase()}</div>
    </div>;
  const half = (src: string, from: number, focus = 50) =>
    <div style={{position: 'absolute', top: 0, bottom: 0, width: '50%', overflow: 'hidden', [from < 0 ? 'left' : 'right']: 0,
      transform: `translateX(${from * (1 - k) * 100}%)`}}>
      <Img src={staticFile(src)} style={{width: '100%', height: '100%', objectFit: 'cover', objectPosition: `${focus}% 50%`,
        transformOrigin: `${focus}% 50%`, transform: `scale(${zoom})`}}/>
      <AbsoluteFill style={{background: 'linear-gradient(180deg, rgba(0,0,0,.55) 0%, transparent 35%)'}}/>
    </div>;
  return <AbsoluteFill style={{background: '#05040e', opacity: out}}>
    {half(c.game, -1, c.focus)}
    {half(c.real, 1)}
    <div style={{position: 'absolute', left: 'calc(50% - 4px)', top: 0, width: 8, height: `${100 * k}%`,
      background: `linear-gradient(180deg, ${PINK}, ${CYAN})`, boxShadow: `0 0 30px ${PINK}`}}/>
    {label('IN GAME', PINK, c.name, false)}
    {label('REAL LIFE', CYAN, c.place, true)}
  </AbsoluteFill>;
};

/** Opening title: the route through every region is drawn on the map, then the title lands. */
export const RouteIntro = ({intro, maps, head, body}: {intro: Intro; maps: MapPoint[]; head: string; body: string}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const len = intro.dur * fps;
  const stops = maps.filter((m, i) => maps.findIndex((x) => x.name === m.name) === i);
  const step = 0.42 * fps;
  const shown = (i: number) => interpolate(f, [i * step, i * step + 8], [0, 1], clamp);
  const reach = Math.min(stops.length - 1, f / step);
  const route = stops.slice(0, Math.floor(reach) + 1).map((s) => [px(s.lon), py(s.lat)]);
  if (reach < stops.length - 1) {
    const a = stops[Math.floor(reach)], b = stops[Math.floor(reach) + 1], t = reach % 1;
    route.push([px(a.lon) + (px(b.lon) - px(a.lon)) * t, py(a.lat) + (py(b.lat) - py(a.lat)) * t]);
  }
  const titleAt = stops.length * step;
  const tk = spring({frame: f - titleAt, fps, config: {damping: 16}});
  const out = interpolate(f, [len - 10, len], [1, 0], clamp);
  const pulse = (f % 40) / 40;
  return <AbsoluteFill style={{background: 'radial-gradient(ellipse at 30% 50%, #141a33 0%, #05040e 75%)', opacity: out}}>
    <div style={{position: 'absolute', left: 40, top: 40, width: 1000, height: 1000,
      transform: `scale(${interpolate(f, [0, len], [1, 1.06], {...clamp, easing: Easing.out(Easing.quad)})})`}}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{width: '100%', height: '100%'}}>
        <defs><filter id="ig"><feGaussianBlur stdDeviation="7"/></filter></defs>
        <path d={LAND} fill="none" stroke={CYAN} strokeWidth={12} opacity={0.35} filter="url(#ig)"/>
        <path d={LAND} fill="#1f2440" stroke={CYAN} strokeWidth={3} strokeLinejoin="round"/>
        {KEYS.map(([lo, la, r], i) => <ellipse key={i} cx={px(lo)} cy={py(la)} rx={r} ry={r * 0.55}
          fill="#1f2440" stroke={CYAN} strokeWidth={2.5}/>)}
        <polyline points={route.map(([x, y]) => `${x},${y}`).join(' ')} fill="none" stroke="white" strokeWidth={4}
          strokeDasharray="12 10" opacity={0.75}/>
        {stops.map((s, i) => {
          const o = shown(i);
          const last = i === stops.length - 1;
          const x = px(s.lon), y = py(s.lat);
          const left = !(s.name.toLowerCase().includes("vice") || y < py(29));
          return <g key={i} opacity={o}>
            {last ? <circle cx={x} cy={y} r={20 + 46 * pulse} fill="none" stroke={PINK} strokeWidth={4} opacity={1 - pulse}/> : null}
            <circle cx={x} cy={y} r={(last ? 18 : 13) * (0.6 + 0.4 * o)} fill={last ? PINK : CYAN} stroke="white" strokeWidth={4}/>
            {last ? <text x={x} y={y + 9} textAnchor="middle" fontFamily={head} fontSize={26} fill="white">?</text> : null}
            <text x={left ? x - 26 : x + 26} y={y + 12} textAnchor={left ? 'end' : 'start'} fontFamily={head} fontSize={38}
              fill="white" stroke="#05040e" strokeWidth={8} paintOrder="stroke">{s.name.toUpperCase()}</text>
          </g>;
        })}
      </svg>
    </div>
    <div style={{position: 'absolute', right: 110, top: 0, bottom: 0, width: 760, display: 'flex',
      flexDirection: 'column', justifyContent: 'center', opacity: Math.min(1, Math.max(0, tk)),
      transform: `translateY(${(1 - tk) * 50}px)`}}>
      <div style={{fontFamily: body, fontWeight: 800, fontSize: 30, letterSpacing: 10, color: CYAN}}>GTA 6 × REAL FLORIDA</div>
      <div style={{fontFamily: head, fontSize: 124, lineHeight: 1.0, color: 'white', marginTop: 12,
        textShadow: `6px 6px 0 ${PINK}, 0 10px 40px rgba(0,0,0,.8)`}}>{intro.title.toUpperCase()}</div>
      {intro.sub ? <div style={{alignSelf: 'flex-start', marginTop: 22, background: PINK, color: 'white', fontFamily: head,
        fontSize: 56, padding: '4px 20px', transform: `rotate(-2deg) scale(${interpolate(f - titleAt, [6, 14], [0, 1], clamp)})`}}>
        {intro.sub.toUpperCase()}</div> : null}
    </div>
  </AbsoluteFill>;
};
