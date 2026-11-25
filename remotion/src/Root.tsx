import {LongForm, Thumbnail, type LongProps} from './LongForm';
import {Composition} from 'remotion';
import {PolishedClip} from './PolishedClip';

export const RemotionRoot = () => <>
<Composition id="LongForm" component={LongForm as never} width={1920} height={1080} fps={30}
  durationInFrames={300}
  calculateMetadata={({props}) => ({durationInFrames: Math.max(30, Math.round((props as LongProps).durationSeconds * 30))})}
  defaultProps={{durationSeconds: 10, narration: '', music: [], musicVolume: 0.1, shots: [], chapters: [],
    lowers: [], cards: [], captions: [], whoosh: '', title: ''} as never}/>
<Composition id="Thumbnail" component={Thumbnail as never} width={1280} height={720} fps={30} durationInFrames={1}
  defaultProps={{bg: '', text: 'Title', accent: ''} as never}/>
<Composition
  id="PolishedClip" component={PolishedClip} width={1080} height={1920}
  fps={30} durationInFrames={1800}
  calculateMetadata={({props}) => {
    if (!Number.isFinite(props.durationSeconds) || props.durationSeconds <= 0 || props.durationSeconds > 60)
      throw new Error('Clip duration must be greater than 0 and at most 60 seconds');
    return {durationInFrames: Math.round(props.durationSeconds * 30)};
  }}
  defaultProps={{source: '', streamer: 'streamer', durationSeconds: 60,
    hook: {text: 'I accidentally booked the flight for next year.',
      evidence: 'I accidentally booked the flight for next year.', method: 'transcript_quote_v1'}, captions: [
    {text: 'Readable', startMs: 0, endMs: 700, timestampMs: null, confidence: null},
    {text: 'word-by-word', startMs: 700, endMs: 1500, timestampMs: null, confidence: null},
    {text: 'captions', startMs: 1500, endMs: 2500, timestampMs: null, confidence: null},
  ]}}
/>
</>;
