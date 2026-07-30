"""Community node (Python satellite): **z-score outlier detector**.

Given a batch of items, it reads a numeric field, computes the mean and standard deviation
across the whole batch with **NumPy**, and marks each item as an outlier when its value lies
more than `threshold` standard deviations from the mean. It adds two fields to every item:
`z_score` (signed, rounded) and `is_outlier` (bool).

This is a genuinely batch operation — the statistics depend on *all* the items — which is
exactly what the node contract's batch delivery is for. It is also a real demonstration of a
node pulling in a native dependency (NumPy), not the standard library.
"""
import numpy as np

from workflow_node import (
    BOOL,
    ConfigField,
    DataField,
    Manifest,
    Node,
    NodeError,
    NUMBER,
    Output,
    STRING,
    TEXT_FIELD,
    NUMBER_FIELD,
    node,
)


class ZScoreOutliers(Node):
    # (1) input contract, (2) output contract, (3) UI/config.
    def manifest(self) -> Manifest:
        return Manifest(
            node_type="zscore_outliers",
            name="Outlier Detector (z-score)",
            version="1.0.0",
            description="Flags numeric outliers by z-score across the batch, using NumPy.",
            icon="activity",
            tags=["science-kit", "statistics", "tools"],
            config_fields=[
                ConfigField(
                    "field", STRING, TEXT_FIELD, required=True,
                    description="Numeric field to analyze",
                ),
                ConfigField(
                    "threshold", NUMBER, NUMBER_FIELD, required=False,
                    description="Flag values this many standard deviations from the mean (default 3)",
                    min=0,
                ),
            ],
            input_fields=[DataField("value", NUMBER)],
            output_fields=[
                DataField("value", NUMBER),
                DataField("z_score", NUMBER),
                DataField("is_outlier", BOOL),
            ],
        )

    # (4) logic. Uses NumPy for mean/std over the whole batch, then annotates each item.
    def logic(self, inputs, config):
        field = config.get("field")
        if not field:
            raise NodeError("MISSING_FIELD", "config 'field' is required")
        threshold = float(config.get("threshold") or 3.0)

        # Collect the numeric values, remembering which items are analyzable. A non-numeric
        # or missing value is not a node failure — that item passes through unflagged.
        values = []
        numeric_idx = []
        for i, item in enumerate(inputs):
            v = item.get(field)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values.append(float(v))
                numeric_idx.append(i)

        out = [dict(item) for item in inputs]
        if not values:
            for item in out:
                item["z_score"] = None
                item["is_outlier"] = False
            return Output.items(out)

        arr = np.asarray(values, dtype=float)
        mean = float(arr.mean())
        std = float(arr.std())

        # A zero-variance batch has no outliers: every z-score is 0. Guard the divide.
        z_scores = np.zeros_like(arr) if std == 0.0 else (arr - mean) / std

        for pos, i in enumerate(numeric_idx):
            z = float(z_scores[pos])
            out[i]["z_score"] = round(z, 4)
            out[i]["is_outlier"] = bool(abs(z) > threshold)
        # Items whose field was missing/non-numeric: annotate consistently.
        for i, item in enumerate(out):
            if "is_outlier" not in item:
                item["z_score"] = None
                item["is_outlier"] = False

        return Output.items(out)


run = node(ZScoreOutliers())
