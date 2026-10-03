from __future__ import annotations

import unittest

from tools.build_video_finish_workflow import build_graph


class BuildVideoFinishWorkflowTests(unittest.TestCase):
    def test_upscale_graph_preserves_timing_and_audio_inputs(self) -> None:
        graph = build_graph("shot.mp4 [output]", "project/shot-upscaled")

        self.assertEqual(graph["load-upscale-model"]["inputs"]["model_name"], "RealESRGAN_x2plus.pth")
        self.assertEqual(graph["create-review-video"]["inputs"]["fps"], ["video-components", 2])
        self.assertEqual(graph["create-review-video"]["inputs"]["audio"], ["video-components", 1])
        self.assertEqual(graph["save-review-video"]["inputs"]["video"], ["create-review-video", 0])
        self.assertNotIn("color-match", graph)

    def test_color_review_is_explicit_and_bounded(self) -> None:
        graph = build_graph(
            "shot.mp4 [output]",
            "project/shot-finished",
            color_reference="keyframe.png [output]",
            color_method="uniform",
            color_strength=0.25,
        )

        self.assertEqual(graph["color-match"]["inputs"]["source_stats"], "uniform")
        self.assertEqual(graph["color-match"]["inputs"]["strength"], 0.25)
        self.assertEqual(graph["create-review-video"]["inputs"]["images"], ["color-match", 0])

    def test_rejects_unbounded_color_strength(self) -> None:
        with self.assertRaises(ValueError):
            build_graph("shot.mp4", "output", color_reference="ref.png", color_strength=1.5)


if __name__ == "__main__":
    unittest.main()
