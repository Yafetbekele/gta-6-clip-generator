import {Composition} from 'remotion';
import {PolishedClip} from './PolishedClip';

export const RemotionRoot = () => <Composition
  id="PolishedClip" component={PolishedClip} width={1080} height={1920}
  fps={30} durationInFrames={1800}
  calculateMetadata={({props}) => {
    if (!Number.isFinite(props.durationSeconds) || props.durationSeconds <= 0 || props.durationSeconds > 60)
      throw new Error('Clip duration must be greater than 0 and at most 60 seconds');
    return {durationInFrames: Math.round(props.durationSeconds * 30)};
  }}
  defaultProps={{source: '', streamer: 'streamer', durationSeconds: 60, captions: [
    {text: 'Readable', startMs: 0, endMs: 700, timestampMs: null, confidence: null},
    {text: 'word-by-word', startMs: 700, endMs: 1500, timestampMs: null, confidence: null},
    {text: 'captions', startMs: 1500, endMs: 2500, timestampMs: null, confidence: null},
  ]}}
/>;
