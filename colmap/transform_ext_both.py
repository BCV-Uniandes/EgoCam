import numpy as np
import tensorflow as tf
import pickle
from typing import Union, Tuple, Dict
from pyntcloud import PyntCloud
import pandas as pd
import os
import tqdm
import open3d as o3d
import argparse

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'


def open3d_outlier_removal(points, nb_neighbors=1, std_ratio=1):
    point_clouds = []
    for pose in points.values():
        center = camera_center_from_extrinsics(pose).numpy()
        point_clouds.append(center)
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(point_clouds)
    cl, ind = pcd.remove_statistical_outlier(nb_neighbors=nb_neighbors, std_ratio=std_ratio)
    new_points = {}
    for i, tupl in enumerate(zip(points.keys(), points.values())):
        key, value = tupl[0], tupl[1]
        if i in ind:
            new_points[key]=value
    print("Original: {}, Filtered: {}".format(len(points), len(new_points)))
    return new_points

def to_rotation_matrix(r: Union[tf.Tensor, np.ndarray]) -> tf.Tensor:
    """Enforces a rotation matrix form of the input.

    Args:
    r: N x N input array. Can be a numpy array or a tf.Tensor

    Returns:
    Orthonormal rotation tensor N x N.
    """
    if r.shape[0] != r.shape[1]:
        raise ValueError('Rotation matrix must be rectangular')

    r = tf.convert_to_tensor(r)

    _, u, v = tf.linalg.svd(r)

    # Handle case where determinant of rotation matrix is negative.
    r = tf.matmul(v, tf.transpose(u))
    correction = tf.cond(
        tf.linalg.det(r) < 0, lambda: tf.linalg.diag([1, 1, -1]),
        lambda: tf.linalg.diag([1, 1, 1]))

    v = tf.matmul(v, tf.cast(correction, v.dtype))
    r = tf.matmul(v, tf.transpose(u))

    return r

def fit_rigid_transform(
    p: Union[tf.Tensor, np.ndarray],
    q: Union[tf.Tensor, np.ndarray]) -> Tuple[tf.Tensor, tf.Tensor, tf.Tensor]:
    """Fit rigid transformation tensors for rotation, translation, and scale.

    Implementation follows:
    1. https://ieeexplore.ieee.org/document/4767965
    2. https://igl.ethz.ch/projects/ARAP/svd_rot.pdf
    See scipy.spatial.procrustes for the black-box method.

    Args:
    p: 3 x N points of source. Can be a numpy array or a tf.Tensor
    q: 3 x N points of destination. Can be a numpy array or a tf.Tensor

    Returns:
    Rotation tensor r, translation tensor t, and scale,
    such that q = tf.matmul(r, scale * p) + t
    """
    
    p = tf.convert_to_tensor(p, dtype=tf.float32)
    q = tf.convert_to_tensor(q, dtype=tf.float32)

    if p.shape[0] != 3 or q.shape[0] != 3:
        print('3D points are required as input.', p.shape[0], q.shape[0])
        return None, None, None
    if p.shape[1] < 3 or q.shape[1] < 3:
        print('3 or more 3D points are required.',p.shape[1], q.shape[1] )
        return None, None, None 

    # Compute source and destination centroids.
    c_p = tf.reduce_mean(p, axis=1, keepdims=True)
    c_q = tf.reduce_mean(q, axis=1, keepdims=True)

    # Compute average distance to centroid for scale
    dist_p = tf.norm(p - tf.tile(c_p, (1, p.shape[1])), axis=0)
    dist_q = tf.norm(q - tf.tile(c_q, (1, q.shape[1])), axis=0)
    scale = tf.divide(
        tf.reduce_mean(dist_q), tf.maximum(tf.reduce_mean(dist_p), 1e-7))

    # Bring source and destination points to origin
    p_norm = tf.multiply(scale, p - c_p)
    q_norm = q - c_q

    # Compute r and t
    h = tf.matmul(p_norm, tf.transpose(q_norm))
    r = to_rotation_matrix(h)
    t = c_q - tf.matmul(r, tf.multiply(scale, c_p))

    return r, t, scale

def camera_center_from_extrinsics(p: tf.Tensor):
    
    """Computes camera center from extrinsics. p is the 3 x 4 extrinsics."""
    r = p[:3, :3]
    t = p[:, -1][..., tf.newaxis]
    return tf.squeeze(-tf.linalg.inv(r) @ t, axis=1)

def gather_mutual_centers(extr_dict_source: Dict[str, tf.Tensor],
                        extr_dict_destination: Dict[str, tf.Tensor]):
    """Returns frame names, camera centers on source, and centers on destination.

    Args:
    extr_dict_source: {frame_name: extrinsics (3 x 4)} mapping for source (COLMAP).
    extr_dict_destination: {frame_name: extrinsics (3 x 4)} mapping for destination (scan).
    """
    print("Colmap: {} / PnP: {}".format(len(extr_dict_source), len(extr_dict_destination)))
    frame_names = sorted(extr_dict_destination)
    centers_source = []
    centers_destination = []
    for frame_name in frame_names:
        if frame_name not in extr_dict_source:
            continue
        p_source = extr_dict_source[frame_name]
        centers_source.append(camera_center_from_extrinsics(p_source))
        p_destination = extr_dict_destination[frame_name]
        centers_destination.append(camera_center_from_extrinsics(p_destination))
    if len(centers_source)==0 or len(centers_destination)==0:
        return frame_names, centers_source, centers_destination
    centers_source =  tf.convert_to_tensor(centers_source, dtype=tf.float32)
    centers_source = tf.transpose(centers_source,[1,0])
    centers_destination = tf.transpose(tf.convert_to_tensor(centers_destination, dtype=tf.float32), [1,0])
    
    return frame_names, centers_source, centers_destination

def translate(t):
    diag =  np.diag([1.0,1.0,1.0,1.0])
    diag[:3,3] = t
    return tf.convert_to_tensor(diag, dtype=tf.float32)

def scale(s):
    s.append(1)
    return tf.convert_to_tensor(np.diag(s), dtype=tf.float32)

def to_homogeneous(r):
    rta = np.zeros((4,4))
    rta[0:3,0:3] =  r
    rta[-1,-1] = 1
    return rta

def get_procrustes_transform(p1, p2):
    """Gets the 7-DOF transformation from p1 to p2."""
    r, t, s = fit_rigid_transform(p1, p2)
    if r is None or t is None or s is None: 
        return None 
    r = to_homogeneous(r) # creates a 4x4 matrix where [0:3, 0:3] is the rotation matrix.
    s = scale([s, s, s]) # Creates a diag([s, s, s, 1]) matrix.
    t = translate(tf.squeeze(t)) # creates a 4 x 4 matrix where [:, 3] is the translation vector.
    return t @ r @s

def transform_points(centers_colmap, T_proc):
    centers_colmap = np.append(centers_colmap, np.ones((1,centers_colmap.shape[1])), axis=0)
    return T_proc @ centers_colmap
def apply_procrustes_to_extrinsics(extrinsic, trans):
    """Transform extrinsics with procrustes."""
    tmp = tf.cast(extrinsic, tf.float32) @ tf.linalg.inv(trans)
    tmp = tmp / tf.reduce_mean(tf.norm(tmp[:3, :3], axis=0))
    return tf.concat((tmp[:3, :], [[0, 0, 0, 1]]), axis=0)

def export_to_pyl(points, colmap, filename):
    points =  points.numpy()
    data = {
        'x':points[0,:],
        'y':points[1,:],
        'z':points[2,:],
        'red': 0.0,
        'green': 0.0,
        'blue': 0.0
    }
    cloud = PyntCloud(pd.DataFrame(data=data, index=[0]))
    filename = f"{filename}.ply"
    cloud.to_file(os.path.join("pruebas_registrations_individuales",filename))
    print("Centers saved at", filename)

if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--split",
        type=str,
        default = "val",
        help="val/test",
    )
    parser.add_argument(
        "--filter",
        action='store_true',
        help="filter outliers",
    )
    parser.add_argument(
        "--path_to_clips_frames",
        type=str,
        default="/media/SSD5/ego4d/dataset/3d/v1/clips_5fps_frames"
        help="Input folder with the clips.",
    )

    args = parser.parse_args()
    
    # Process clip branch

    colmap_name = "colmap_ext_val.pkl" if args.split=="val" else "colmap_ext_test.pkl"
    pnp_name =  "pnp_ext_val.pkl" if args.split=="val" else "pnp_ext_test.pkl"
    extr_dict_colmap_total = pickle.load(open(colmap_name, "rb"))
    extr_dict_pnp_total = pickle.load(open(pnp_name, "rb"))

    path_to_clips_frames = args.path_to_clips_frames
    
    root_dir ="/media/SSD5/ego4d/dataset/3d/v1/clips_camera_poses_5fps" if args.split=="val" else "/media/SSD5/ego4d/dataset/3d/v1/clips_camera_poses_5fps_test"
    
    valid_clips = 0
    total_clips = 0
    total_frames = 0
    valid_frames = 0

    clips_dict={}
    for clip in extr_dict_colmap_total:
        total_clips+=1
        total_frames += len(os.listdir(os.path.join(path_to_clips_frames, clip)))
        extr_dict_colmap = extr_dict_colmap_total[clip]
        extr_dict_pnp = extr_dict_pnp_total[clip]

        if args.filter:
            #Filter outliers
            extr_dict_colmap=open3d_outlier_removal(extr_dict_colmap, nb_neighbors=15, std_ratio=3)
            extr_dict_pnp=open3d_outlier_removal(extr_dict_pnp, nb_neighbors=15, std_ratio=3)

        if len(extr_dict_pnp)==0:
            continue
        # Compute transformations of camera centers with procrustes.
        # We assume that the mapping of {frame_name: extrinsics (3 x 4)} is given both for colmap and the scan.
        
        frame_names, centers_colmap, centers_pnp = gather_mutual_centers(extr_dict_colmap, extr_dict_pnp)
        if len(centers_colmap)==0 or len(centers_pnp)==0:
            continue

        T_proc = get_procrustes_transform(centers_colmap, centers_pnp)
        if T_proc is None:
            continue

        centers_colmap_onscan = transform_points(centers_colmap, T_proc)
        
        extr_colmap_onscan = []
        valid_poses = []

        for i in tqdm.tqdm(range(len(os.listdir(os.path.join(path_to_clips_frames, clip))))):
            if 'color_%07d.jpg'%i in extr_dict_colmap:
                extr_colmap = tf.concat((extr_dict_colmap['color_%07d.jpg'%i], [[0, 0, 0, 1]]), axis=0)
                extr_colmap_onscan.append(apply_procrustes_to_extrinsics(extr_colmap, T_proc))
                valid_poses.append(True)
            else:
                valid_poses.append(False)
                extr_colmap_onscan.append(np.array([[  1.,   0.,   0.,   0.],
                                                    [  0.,   1.,   0.,   0.],
                                                    [  0.,   0.,   1., 100.],
                                                    [  0.,   0.,   0.,   1.]]))
        if sum(valid_poses)>0:
            clips_dict[clip] = valid_poses
    

    #Process scan branch
    clips_scan_dict = {}

    colmap_name = "colmap_ext_val_scan.pkl" if args.split=="val" else "colmap_ext_test_scan.pkl"
    pnp_name = "pnp_ext_val_scan.pkl" if args.split=="val" else "pnp_ext_test_scan.pkl"
    extr_dict_colmap_total = pickle.load(open(colmap_name, "rb"))
    extr_dict_pnp_total = pickle.load(open(pnp_name, "rb"))   
    path_to_clips_frames = args.path_to_clips_frames    

    root_dir = "/media/SSD5/ego4d/dataset/3d/v1/clips_camera_poses_5fps" if args.split=="val" else "/media/SSD5/ego4d/dataset/3d/v1/clips_camera_poses_5fps_test"

    for scan in extr_dict_colmap_total:
        extr_dict_colmap = extr_dict_colmap_total[scan]
        extr_dict_pnp = extr_dict_pnp_total[scan]

        if args.filter:
            #filter outliers
            extr_dict_colmap=open3d_outlier_removal(extr_dict_colmap,nb_neighbors=5, std_ratio=4)
            extr_dict_pnp=open3d_outlier_removal(extr_dict_pnp,nb_neighbors=5, std_ratio=4)
        
        if len(extr_dict_pnp)==0:
            continue
        
        # Compute transformations of camera centers with procrustes.
        # We assume that the mapping of {frame_name: extrinsics (3 x 4)} is given both for colmap and the scan.
        
        frame_names, centers_colmap, centers_pnp = gather_mutual_centers(extr_dict_colmap, extr_dict_pnp)
        if len(centers_colmap)==0 or len(centers_pnp)==0:
            continue

        T_proc = get_procrustes_transform(centers_colmap, centers_pnp)
        
        if T_proc is None:
            continue
        
        centers_colmap_onscan = transform_points(centers_colmap, T_proc)

        clips = os.listdir(os.path.join("/media/SSD5/ego4d/dataset/3d/v1/colmap_scan", scan, "database")) if args.split=="val" else os.listdir(os.path.join("/media/SSD5/ego4d/dataset/3d/v1/colmap_scan_test", scan, "database"))
        for clip in clips:
            extr_colmap_onscan = []
            valid_poses = []
            for i in tqdm.tqdm(range(len(os.listdir(os.path.join(path_to_clips_frames, clip))))):
                if os.path.join(clip,'color_%07d.jpg'%i) in extr_dict_colmap:
                    extr_colmap = tf.concat((extr_dict_colmap[os.path.join(clip,'color_%07d.jpg'%i)], [[0, 0, 0, 1]]), axis=0)
                    extr_colmap_onscan.append(apply_procrustes_to_extrinsics(extr_colmap, T_proc))
                    valid_poses.append(True)
                else:
                    valid_poses.append(False)
                    extr_colmap_onscan.append(np.array([[  1.,   0.,   0.,   0.],
                                                    [  0.,   1.,   0.,   0.],
                                                    [  0.,   0.,   1., 100.],
                                                    [  0.,   0.,   0.,   1.]]))
            if sum(valid_poses)>0:
                clips_scan_dict[clip]=valid_poses
    
    # Compute metrics for both
    valid_clips = 0
    valid_frames = 0
    for clip in clips_scan_dict:
        if clip not in clips_dict:
            valid_frames += sum(clips_scan_dict[clip])
            valid_clips+=1
    for clip in clips_dict:
        valid_clips+=1
        for i in range(len(clips_dict[clip])):
            if clips_dict[clip][i]:
                valid_frames+=1
            else:
                if clip in clips_scan_dict:
                    if clips_scan_dict[clip][i]:
                        valid_frames+=1
                              
    print("Clips: {}/{} ({}), Frames {}/{} ({})".format(valid_clips, total_clips, valid_clips*100/total_clips, valid_frames, total_frames, valid_frames*100/total_frames))
