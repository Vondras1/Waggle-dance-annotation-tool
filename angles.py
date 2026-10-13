import math



def correct_angle(angle, df_id):
    if angle < 0:
        angle += math.pi

    if 0 <= angle < (1 / 4) * math.pi:
        x_diff = df_id.iloc[-1]['center_x'] - df_id.iloc[0]['center_x']
        angle += math.pi / 2 if x_diff > 0 else -math.pi / 2

    elif (1 / 4) * math.pi <= angle < (1 / 2) * math.pi:
        y_diff = df_id.iloc[-1]['center_y'] - df_id.iloc[0]['center_y']
        angle += math.pi / 2 if y_diff > 0 else -math.pi / 2

    elif math.pi / 2 <= angle < (3 / 4) * math.pi:
        y_diff = df_id.iloc[-1]['center_y'] - df_id.iloc[0]['center_y']
        angle += -math.pi / 2 - math.pi if y_diff > 0 else -math.pi / 2

    else:
        x_diff = df_id.iloc[-1]['center_x'] - df_id.iloc[0]['center_x']
        angle += -3 * math.pi / 2 if x_diff <= 0 else -math.pi / 2

    return angle


def thieve_deciles(group, decile_1=0, decile_2=100):
    """
        filtering detections between two defined deciles within runs
    """
    n = len(group)
    start = int(n * decile_1 / 100)
    end = int(n * decile_2 / 100)
    return group.iloc[start:end]


def get_angles(detections, decile_1=0, decile_2=100):
    """
        Run a PCA to find run direction vectors.
    """

    import numpy as np
    from sklearn.decomposition import PCA

    detections['center_x'], detections['center_y'] = ((detections['x0'] + detections['x1']) / 2,
                                                      (detections['y0'] + detections['y1']) / 2)

    angle_dict = {}
    for run_id, group in detections.groupby('run_id', sort=False):
        df_id = thieve_deciles(group.sort_values('frame_id'), decile_1, decile_2)
        centers = df_id[['center_x', 'center_y']].to_numpy()
        if len(centers) < 2 or np.allclose(centers, centers[0], rtol=0, atol=1e-12):
            angle_dict[run_id] = np.nan  # No measurable motion direction.
            continue
        pca = PCA(n_components=1)
        pca.fit(centers)
        PC1 = pca.components_[0]
        angle = np.arctan2(PC1[1], PC1[0])
        angle_dict[run_id] = correct_angle(angle, df_id)

    detections['angle'] = detections['run_id'].map(angle_dict)
    return detections.drop(['center_x', 'center_y'], axis=1)


def direction_from_centers(centers):
    """PCA direction in signed degrees clockwise from image-up.

    Equivalent 2-D principal-axis fit without pandas/sklearn dependencies.
    Temporal displacement resolves the axis sign. Degenerate trajectories
    return None rather than inventing a direction.
    """
    if len(centers) < 2:
        return None
    mx = sum(p[0] for p in centers) / len(centers)
    my = sum(p[1] for p in centers) / len(centers)
    xx = sum((x - mx) ** 2 for x, y in centers)
    yy = sum((y - my) ** 2 for x, y in centers)
    xy = sum((x - mx) * (y - my) for x, y in centers)
    if xx + yy < 1e-12 or math.hypot(xx - yy, 2 * xy) <= 1e-12 * (xx + yy):
        return None
    theta = 0.5 * math.atan2(2 * xy, xx - yy)
    dx, dy = math.cos(theta), math.sin(theta)
    motion = (centers[-1][0] - centers[0][0]) * dx + (centers[-1][1] - centers[0][1]) * dy
    if abs(motion) < 1e-9:
        return None
    if motion < 0:
        dx, dy = -dx, -dy
    return (math.degrees(math.atan2(dx, -dy)) + 180) % 360 - 180
