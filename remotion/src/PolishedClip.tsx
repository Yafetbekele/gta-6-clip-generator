import {AbsoluteFill, Interactive, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {Video} from '@remotion/media';
import type {Caption} from '@remotion/captions';
import {fillTextBox} from '@remotion/layout-utils';
import {useMemo} from 'react';

type HookData = {text: string; evidence: string; method: string; label?: string};
export type ClipProps = {source: string; streamer: string; durationSeconds: number; captions: Caption[]; hook?: HookData};

const ContextText = ({hook}: {hook?: HookData}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const text = hook?.method?.endsWith('quote_v1') ? `“${hook.text}”` : hook?.text || '';
  const fontSize = useMemo(() => {
    for (let size = 60; size >= 32; size -= 2) {
      const box = fillTextBox({maxBoxWidth: 820, maxLines: 3});
      if (text.split(/\s+/).every((word) => !box.add({text: word + ' ', fontFamily: 'Arial',
        fontWeight: '800', fontSize: size}).exceedsBox)) return size;
    }
    return 32;
  }, [text]);
  if (!hook?.text || frame >= 7 * fps) return null;
  return <Interactive.Div name="Context hook" style={{position: 'absolute', top: 175, left: 90,
    width: 820, fontSize, lineHeight: 1.15, fontWeight: 800, color: '#ffffff',
    overflowWrap: 'anywhere',
    opacity: interpolate(frame, [0, 0.3 * fps, 6.6 * fps, 7 * fps], [0, 1, 1, 0],
      {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'})}}>
    {hook.label ? <div style={{fontSize: 34, letterSpacing: 2, color: '#c4ff58', marginBottom: 10}}>
      {hook.label}</div> : null}
    {text}
  </Interactive.Div>;
};

// Whisper captions are generated data, controlled as a single template layer.
const WordCaptions = ({captions}: {captions: Caption[]}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const now = frame * 1000 / fps;
  const pages: Caption[][] = [];
  for (const caption of captions) {
    const page = pages[pages.length - 1];
    if (!page || page.length >= 5 || caption.startMs - page[page.length - 1].endMs > 450 ||
        page.map((word) => word.text).join(' ').length + caption.text.length > 36) pages.push([caption]);
    else page.push(caption);
  }
  const page = pages.find((words) => now >= words[0].startMs && now < words[words.length - 1].endMs);
  if (!page) return null;
  return <div style={{position: 'absolute', top: 1330, left: 90, width: 820,
    textAlign: 'center', fontSize: 64, fontWeight: 800, lineHeight: 1.35,
    overflowWrap: 'anywhere', textShadow: '0 3px 12px #000'}}>
    {page.map((word, index) => <span key={index} style={{color:
      now >= word.startMs && now < word.endMs ? '#c4ff58' : '#ffffff'}}>{word.text.trim()}{' '}</span>)}
  </div>;
};

const Credit = ({streamer}: {streamer: string}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  return <Interactive.Div name="Streamer credit" style={{position: 'absolute', left: 90,
    top: 440, width: 820, color: '#ffffff', fontSize: 44, fontWeight: 700, overflowWrap: 'anywhere',
    opacity: interpolate(frame, [0, 0.35 * fps], [0, 1], {extrapolateRight: 'clamp'}),
    translate: interpolate(frame, [0, 0.35 * fps], ['0px 18px', '0px 0px'], {extrapolateRight: 'clamp'})}}>
    @{streamer} on Twitch
  </Interactive.Div>;
};

const Progress = () => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  return <Interactive.Div name="Progress line" style={{position: 'absolute', top: 1240,
    left: 40, height: 4, backgroundColor: '#c4ff58',
    width: interpolate(frame, [0, durationInFrames - 1], [0, 1000], {extrapolateRight: 'clamp'})}} />;
};

export const PolishedClip = ({source, streamer, captions, hook}: ClipProps) => (
  <AbsoluteFill style={{backgroundColor: '#0b0d16', fontFamily: 'Arial, sans-serif'}}>
    <Interactive.Div name="Backdrop" style={{position: 'absolute', inset: 0,
      background: 'radial-gradient(ellipse at 15% 35%, #30224f 0%, #101525 45%, #090b12 85%)'}} />
    <Interactive.Div name="Source label" style={{position: 'absolute', left: 90, top: 110,
      fontSize: 26, letterSpacing: 6, color: '#c4ff58', fontWeight: 700}}>STREAM HIGHLIGHT</Interactive.Div>
    <Credit streamer={streamer} />
    <ContextText hook={hook} />
    {/* Full frame width: the whole stream stays visible at the largest uncropped size. */}
    <div style={{position: 'absolute', left: 0, top: 560, width: 1080, height: 680,
      backgroundColor: '#000000', boxShadow: '0 30px 100px #00000080'}}>
      {source ? <Video name="Full stream frame" src={staticFile(source)}
        style={{width: '100%', height: '100%'}} objectFit="contain" /> :
        <div style={{color: '#a6afc4', fontSize: 40, textAlign: 'center', paddingTop: 280}}>
          Full stream footage appears here
        </div>}
    </div>
    <Progress />
    <WordCaptions captions={captions} />
  </AbsoluteFill>
);
