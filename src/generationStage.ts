import type { PipelineStage } from './data';

/** Choose the installed video recipe; a selected preset wins when several recipes coexist. */
export function selectVideoGenerationStage(stages: PipelineStage[], ...preferredWorkflows: Array<string | undefined>): PipelineStage | undefined {
  const videoStages = stages.filter(stage => stage.execution?.mode === 'comfyui'
    && (stage.outputs.some(output => output.includes('视频')) || stage.outputSpecs?.some(output => output.kind === '视频')));
  for (const workflow of preferredWorkflows) {
    const match = videoStages.find(stage => workflow && stage.execution?.references?.includes(workflow));
    if (match) return match;
  }
  return videoStages[0];
}
