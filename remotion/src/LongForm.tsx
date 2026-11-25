import {AbsoluteFill, Audio, Img, OffthreadVideo, Sequence, interpolate, spring, staticFile, useCurrentFrame,
  useVideoConfig, Easing} from 'remotion';
import {loadFont as loadAnton} from '@remotion/google-fonts/Anton';
import {loadFont as loadInter} from '@remotion/google-fonts/Inter';
import {LeonidaMap, type MapPoint} from './LeonidaMap';
import {CompareCard, RouteIntro, type Compare, type Intro} from './Overlays';

const {fontFamily: HEAD} = loadAnton();
const {fontFamily: BODY} = loadInter('normal', {weights: ['600', '800']});
const PINK = '#ff4fa3';
const CYAN = '#38e8ff';
const FADE = 8; // frames of cross-dissolve between shots

type Shot = {kind: 'video' | 'image'; src: string; start: number; dur: number; motion?: string; fit?: 'frame'};
type Timed = {text: string; start: number; dur: number};
type Chapter = {title: string; number: number; start: number};
type Track = {src: string; start: number; dur: number};
export type LongProps = {durationSeconds: number; narration: string; music: Track[]; musicVolume: number;
  shots: Shot[]; chapters: Chapter[]; lowers: Timed[]; cards: Timed[];
  captions: {text: string; s: number; e: number}[]; whoosh: string; title: string; maps?: MapPoint[];
  compares?: Compare[]; intro?: Intro | null};

const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

const KenBurns = ({src, frames, motion}: {src: string; frames: number; motion?: string}) => {
  const f = useCurrentFrame();
  const p = interpolate(f, [0, frames], [0, 1], {...clamp, easing: Easing.inOut(Easing.sin)});
  const scale = motion === 'out' ? 1.16 - 0.1 * p : 1.06 + 0.1 * p;
  const x = motion === 'left' ? 30 - 60 * p : motion === 'right' ? -30 + 60 * p : 0;
  return <AbsoluteFill style={{overflow: 'hidden'}}>
    <Img src={staticFile(src)} style={{width: '100%', height: '100%', objectFit: 'cover',
      transform: `scale(${scale}) translateX(${x}px)`, filter: 'contrast(1.05) saturate(1.08)'}}/>
  </AbsoluteFill>;
};

/** A real photo of any shape: whole, with a white border and shadow, on a dark blurred copy of itself. */
const FramedPhoto = ({src, frames}: {src: string; frames: number}) => {
  const f = useCurrentFrame();
  const p = interpolate(f, [0, frames], [0, 1], {...clamp, easing: Easing.inOut(Easing.sin)});
  return <AbsoluteFill style={{overflow: 'hidden', background: '#05040e'}}>
    <Img src={staticFile(src)} style={{width: '100%', height: '100%', objectFit: 'cover', transform: 'scale(1.2)',
      filter: 'blur(40px) brightness(0.45) saturate(0.8)'}}/>
    <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center', paddingBottom: 150, paddingTop: 30}}>
      <Img src={staticFile(src)} style={{maxWidth: '82%', maxHeight: '100%', objectFit: 'contain', border: '10px solid #f4efe4',
        boxShadow: '0 30px 80px rgba(0,0,0,.75)', transform: `scale(${1 + 0.06 * p}) rotate(${-0.6 + 1.2 * p}deg)`}}/>
    </AbsoluteFill>
  </AbsoluteFill>;
};

const ShotLayer = ({shot}: {shot: Shot}) => {
  const {fps} = useVideoConfig();
  const f = useCurrentFrame();
  const frames = Math.round(shot.dur * fps) + FADE;
  const opacity = interpolate(f, [0, FADE], [0, 1], clamp);
  return <AbsoluteFill style={{opacity}}>
    {shot.kind === 'image' && shot.fit === 'frame' ? <FramedPhoto src={shot.src} frames={frames}/>
      : shot.kind === 'image' ? <KenBurns src={shot.src} frames={frames} motion={shot.motion}/>
      : <OffthreadVideo src={staticFile(shot.src)} muted style={{width: '100%', height: '100%', objectFit: 'cover'}}/>}
  </AbsoluteFill>;
};

const ChapterCard = ({c, maps, mapIdx}: {c: Chapter; maps: MapPoint[]; mapIdx: number}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const len = 2.6 * fps;
  const inn = spring({frame: f, fps, config: {damping: 200}});
  const out = interpolate(f, [len - 10, len], [1, 0], clamp);
  const hasMap = mapIdx >= 0;
  return <AbsoluteFill style={{background: `rgba(5,4,14,${(hasMap ? 0.82 : 0.72) * out})`, flexDirection: 'row',
    justifyContent: 'center', alignItems: 'center'}}>
    <div style={{opacity: out, transform: `translateY(${(1 - inn) * 40}px)`, textAlign: hasMap ? 'left' : 'center',
      width: hasMap ? 860 : undefined, marginRight: hasMap ? 40 : 0}}>
      <div style={{fontFamily: BODY, fontWeight: 800, fontSize: 30, letterSpacing: 10, color: CYAN}}>
        CHAPTER {c.number}</div>
      <div style={{fontFamily: HEAD, fontSize: hasMap ? 116 : 128, color: 'white', lineHeight: 1.05, marginTop: 10,
        textShadow: '0 6px 30px rgba(0,0,0,.6)', maxWidth: 1500}}>{c.title.toUpperCase()}</div>
      <div style={{height: 6, width: 420 * inn, background: `linear-gradient(90deg, ${PINK}, ${CYAN})`,
        margin: hasMap ? '26px 0 0' : '26px auto 0', borderRadius: 3}}/>
      {hasMap ? <div style={{fontFamily: BODY, fontWeight: 600, fontSize: 34, color: 'rgba(255,255,255,.85)', marginTop: 24}}>
        Real world: <span style={{color: PINK, fontWeight: 800}}>{maps[mapIdx].place}</span></div> : null}
    </div>
    {hasMap ? <div style={{width: 820, height: 810, opacity: out, transform: `scale(${0.92 + 0.08 * inn})`}}>
      <LeonidaMap points={maps} idx={mapIdx} t={f} big/></div> : null}
  </AbsoluteFill>;
};

const MiniMap = ({maps, idx, t, opacity}: {maps: MapPoint[]; idx: number; t: number; opacity: number}) => {
  const {fps} = useVideoConfig();
  const k = spring({frame: t, fps, config: {damping: 200}});
  const p = maps[idx];
  return <div style={{position: 'absolute', right: 48, top: 44, width: 300, opacity}}>
    <div style={{width: 300, height: 296, background: 'rgba(8,6,20,.72)', borderRadius: 18,
      border: '2px solid rgba(56,232,255,.4)', padding: 10, boxSizing: 'border-box'}}>
      <LeonidaMap points={maps} idx={idx} t={t}/></div>
    <div style={{marginTop: 10, background: 'rgba(8,6,20,.82)', borderRadius: 12, padding: '8px 14px',
      borderLeft: `6px solid ${PINK}`, transform: `translateX(${(1 - k) * 20}px)`}}>
      <div style={{fontFamily: HEAD, fontSize: 32, color: 'white', lineHeight: 1.1}}>{p.name.toUpperCase()}</div>
      <div style={{fontFamily: BODY, fontWeight: 600, fontSize: 21, color: CYAN}}>{p.place}</div>
    </div>
  </div>;
};

const LowerThird = ({t}: {t: Timed}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const len = t.dur * fps;
  const inn = spring({frame: f, fps, config: {damping: 18, stiffness: 120}});
  const out = interpolate(f, [len - 12, len], [1, 0], clamp);
  return <AbsoluteFill>
    <div style={{position: 'absolute', left: 90, bottom: 120, display: 'flex', opacity: out,
      transform: `translateX(${(1 - inn) * -60}px)`}}>
      <div style={{width: 8, background: PINK, borderRadius: 4}}/>
      <div style={{background: 'rgba(8,6,20,.82)', padding: '18px 30px', fontFamily: BODY, fontWeight: 800,
        fontSize: 40, color: 'white', maxWidth: 1100, clipPath: `inset(0 ${(1 - inn) * 100}% 0 0)`}}>{t.text}</div>
    </div>
  </AbsoluteFill>;
};

const BigCard = ({t}: {t: Timed}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const len = t.dur * fps;
  const inn = spring({frame: f, fps, config: {damping: 14}});
  const out = interpolate(f, [len - 10, len], [1, 0], clamp);
  return <AbsoluteFill style={{background: `radial-gradient(circle, rgba(0,0,0,${0.55 * out}) 0%, rgba(0,0,0,${0.35 * out}) 70%)`,
    justifyContent: 'center', alignItems: 'center'}}>
    <div style={{fontFamily: HEAD, fontSize: 150, color: 'white', opacity: out, textAlign: 'center', maxWidth: 1600,
      transform: `scale(${0.85 + 0.15 * inn})`, textShadow: `0 0 40px ${PINK}88, 0 8px 30px rgba(0,0,0,.7)`,
      lineHeight: 1.05}}>{t.text}</div>
  </AbsoluteFill>;
};

const Captions = ({items}: {items: LongProps['captions']}) => {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const t = f / fps;
  const c = items.find((x) => t >= x.s && t <= x.e + 0.2);
  if (!c) return null;
  return <div style={{position: 'absolute', bottom: 70, width: '100%', textAlign: 'center'}}>
    <span style={{fontFamily: BODY, fontWeight: 800, fontSize: 46, color: 'white', background: 'rgba(0,0,0,.55)',
      padding: '6px 18px', borderRadius: 8}}>{c.text}</span></div>;
};

export const LongForm = (p: LongProps) => {
  const {fps, durationInFrames} = useVideoConfig();
  const frame = useCurrentFrame();
  const fr = (s: number) => Math.round(s * fps);
  const inCard = (t: number) => p.chapters.some((c) => t >= c.start - 0.2 && t <= c.start + 2.4)
    || (!!p.intro && t >= p.intro.start && t <= p.intro.start + p.intro.dur);
  const maps = p.maps ?? [];
  const now = frame / fps;
  let mapIdx = -1;
  maps.forEach((m, i) => { if (m.start <= now) mapIdx = i; });
  const cardAt = (t: number) => p.chapters.find((c) => t >= c.start - 0.1 && t <= c.start + 2.6);
  const chapterMap = (c: Chapter) => maps.findIndex((m) => Math.abs(m.start - c.start) < 0.05);
  const card = cardAt(now);
  const covered = (p.compares ?? []).some((c) => now >= c.start - 0.1 && now <= c.start + c.dur)
    || (!!p.intro && now >= p.intro.start && now <= p.intro.start + p.intro.dur);
  const miniOpacity = mapIdx < 0 || covered ? 0 : card && chapterMap(card) >= 0
    ? interpolate(now - card.start, [2.2, 2.6], [0, 1], clamp) : 1;
  const fadeOut = interpolate(frame, [durationInFrames - fr(1.5), durationInFrames], [0, 1], clamp);
  return <AbsoluteFill style={{background: 'black'}}>
    {p.shots.map((s, i) => <Sequence key={i} from={fr(s.start)} durationInFrames={fr(s.dur) + FADE}>
      <ShotLayer shot={s}/></Sequence>)}
    <AbsoluteFill style={{background: 'radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,.55) 100%)'}}/>
    {p.lowers.map((t, i) => <Sequence key={i} from={fr(t.start)} durationInFrames={fr(t.dur)}><LowerThird t={t}/></Sequence>)}
    {p.cards.map((t, i) => <Sequence key={i} from={fr(t.start)} durationInFrames={fr(t.dur)}><BigCard t={t}/></Sequence>)}
    {(p.compares ?? []).map((c, i) => <Sequence key={'vs' + i} from={fr(c.start)} durationInFrames={fr(c.dur)}>
      <CompareCard c={c} head={HEAD} body={BODY}/><Audio src={staticFile(p.whoosh)} volume={0.35}/></Sequence>)}
    {p.intro ? <Sequence from={fr(p.intro.start)} durationInFrames={fr(p.intro.dur)}>
      <RouteIntro intro={p.intro} maps={maps} head={HEAD} body={BODY}/><Audio src={staticFile(p.whoosh)} volume={0.5}/></Sequence> : null}
    {p.chapters.map((c, i) => <Sequence key={i} from={fr(c.start)} durationInFrames={fr(2.6)}>
      <ChapterCard c={c} maps={maps} mapIdx={chapterMap(c)}/><Audio src={staticFile(p.whoosh)} volume={0.5}/></Sequence>)}
    {mapIdx >= 0 ? <MiniMap maps={maps} idx={mapIdx} t={frame - fr(maps[mapIdx].start)} opacity={miniOpacity}/> : null}
    {p.captions.length ? <Captions items={p.captions}/> : null}
    <AbsoluteFill style={{background: 'black', opacity: fadeOut}}/>
    <Audio src={staticFile(p.narration)}/>
    {p.music.map((m, i) => <Sequence key={i} from={fr(m.start)} durationInFrames={fr(m.dur)}>
      <Audio src={staticFile(m.src)} volume={(f) => {
        const t = m.start + f / fps;
        const edge = interpolate(f, [0, fr(2), fr(m.dur) - fr(2), fr(m.dur)], [0, 1, 1, 0], clamp);
        return edge * (inCard(t) ? p.musicVolume * 2.8 : p.musicVolume);
      }}/></Sequence>)}
  </AbsoluteFill>;
};

/** Split thumbnail: game shot left, real photo right, a slanted neon seam, the hook text along the bottom. */
const SplitThumb = ({left, right, text, accent}: {left: string; right: string; text: string; accent: string}) => {
  const tag = (label: string, color: string, side: 'left' | 'right') =>
    <div style={{position: 'absolute', top: 34, [side]: 34, background: color, color: '#0b0b18', fontFamily: BODY,
      fontWeight: 900, fontSize: 46, letterSpacing: 4, padding: '4px 20px', borderRadius: 8,
      boxShadow: '0 6px 24px rgba(0,0,0,.6)', transform: `rotate(${side === 'left' ? -3 : 3}deg)`}}>{label}</div>;
  const pic = (src: string, clip: string) => <AbsoluteFill style={{clipPath: clip}}>
    <Img src={staticFile(src)} style={{width: '100%', height: '100%', objectFit: 'cover',
      filter: 'contrast(1.12) saturate(1.3)'}}/></AbsoluteFill>;
  return <AbsoluteFill style={{background: '#05040e'}}>
    {pic(left, 'polygon(0 0, 56% 0, 44% 100%, 0 100%)')}
    {pic(right, 'polygon(56% 0, 100% 0, 100% 100%, 44% 100%)')}
    <svg viewBox="0 0 1280 720" style={{position: 'absolute', inset: 0}}>
      <line x1={717} y1={-10} x2={563} y2={730} stroke={PINK} strokeWidth={26} opacity={0.5} style={{filter: 'blur(10px)'}}/>
      <line x1={717} y1={-10} x2={563} y2={730} stroke="white" strokeWidth={10}/>
    </svg>
    {tag('GAME', PINK, 'left')}
    {tag('REAL', CYAN, 'right')}
    <AbsoluteFill style={{background: 'linear-gradient(0deg, rgba(0,0,0,.85) 0%, transparent 38%)'}}/>
    <div style={{position: 'absolute', left: 0, right: 0, bottom: 34, display: 'flex', flexDirection: 'column',
      alignItems: 'center'}}>
      {accent ? <div style={{background: PINK, color: 'white', fontFamily: BODY, fontWeight: 800, fontSize: 34,
        padding: '4px 16px', marginBottom: 10, transform: 'rotate(-2deg)'}}>{accent.toUpperCase()}</div> : null}
      <div style={{fontFamily: HEAD, fontSize: 112, lineHeight: 1.0, color: 'white', WebkitTextStroke: '3px black',
        textShadow: `6px 6px 0 ${PINK}, 0 10px 40px rgba(0,0,0,.8)`}}>{text.toUpperCase()}</div>
    </div>
  </AbsoluteFill>;
};

export const Thumbnail = ({bg, text, accent, left, right}: {bg: string; text: string; accent: string; left?: string; right?: string}) =>
  left && right ? <SplitThumb left={left} right={right} text={text} accent={accent}/> :
  <AbsoluteFill>
    <Img src={staticFile(bg)} style={{width: '100%', height: '100%', objectFit: 'cover', transform: 'scale(1.08)',
      filter: 'contrast(1.15) saturate(1.3)'}}/>
    <AbsoluteFill style={{background: 'linear-gradient(90deg, rgba(0,0,0,.85) 0%, rgba(0,0,0,.4) 50%, transparent 75%)'}}/>
    <div style={{position: 'absolute', left: 60, top: 0, bottom: 0, width: 720, display: 'flex',
      flexDirection: 'column', justifyContent: 'center'}}>
      {accent ? <div style={{alignSelf: 'flex-start', background: PINK, color: 'white', fontFamily: BODY,
        fontWeight: 800, fontSize: 40, padding: '6px 18px', marginBottom: 18, transform: 'rotate(-2deg)'}}>
        {accent.toUpperCase()}</div> : null}
      <div style={{fontFamily: HEAD, fontSize: 118, lineHeight: 1.0, color: 'white',
        WebkitTextStroke: '3px black', textShadow: `6px 6px 0 ${PINK}, 0 10px 40px rgba(0,0,0,.8)`}}>
        {text.toUpperCase()}</div>
    </div>
  </AbsoluteFill>;
