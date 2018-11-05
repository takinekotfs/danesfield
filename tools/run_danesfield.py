#!/usr/bin/env python

"""
Run the Danesfield processing pipeline on an AOI from start to finish.
"""

import argparse
import configparser
import crop_and_pansharpen
import datetime
import glob
import logging
import os
import re
import sys

# import other tools
import generate_dsm
import fit_dtm
import material_classifier
import orthorectify
import compute_ndvi
import segment_by_height
import texture_mapping
import roof_geon_extraction
import buildings_to_dsm
import get_road_vector
import run_metrics


def create_working_dir(working_dir, imagery_dir):
    """
    Create working directory for running algorithms
    All files generated by the system are written to this directory.

    :param working_dir: Directory to create for work. Cannot be a subdirectory of `imagery_dir`.
    This is to avoid adding the work images to the pipeline when traversing the `imagery_dir`.
    :type working_dir: str

    :param imagery_dir: Directory where imagery is stored.
    :type imagery_dir: str

    :raises ValueError: If `working_dir` is a subdirectory of `imagery_dir`.
    """
    if not working_dir:
        date_str = str(datetime.datetime.now().timestamp())
        working_dir = 'danesfield-' + date_str.split('.')[0]
    if not os.path.isdir(working_dir):
        os.mkdir(working_dir)
    if os.path.realpath(imagery_dir) in os.path.realpath(working_dir):
        raise ValueError('The working directory ({}) is a subdirectory of the imagery directory '
                         '({}).'.format(working_dir, imagery_dir))
    return working_dir


def ensure_complete_modality(modality_dict, require_rpc=False):
    """
    Ensures that a certain modality (MSI, PAN, SWIR) has all of the required files for computation
    through the whole pipeline.

    :param modality_dict: Mapping of a certain modality to its image, rpc, and info files.
    :type modality_dict: dict

    :param require_rpc: Whether or not to consider the rpc file being present as a requirement for
    a complete modality.
    :type require_rpc: bool
    """
    keys = ['image', 'info']
    if require_rpc:
        keys.append('rpc')
    return all(key in modality_dict for key in keys)


def classify_fpaths(prefix, fpaths, id_to_files, file_type):
    """
    Classify the modality of the paths in ``fpaths`` and store that information in
    ``id_to_files_dict``.

    :param prefix: The prefix to use when validating and classifying the paths in ``fpaths``.
    :type prefix: str

    :param fpaths: The filepaths to classify.
    :type fpaths: [str]

    :param id_to_files: Mapping of the prefix ID to it's associated files.
    :type id_to_files: dict

    :param file_type: The type of file to look for. One of `image`, `info`, or `rpc`. This is the
    key used when updating ``id_to_files_dict``.
    :type file_type: str
    """
    for fpath in fpaths:
        if prefix in fpath:
            if '-P1BS-' in fpath:
                id_to_files[prefix]['pan'][file_type] = fpath
            elif '-M1BS-' in fpath:
                id_to_files[prefix]['msi'][file_type] = fpath
            elif '-A1BS-' in fpath:
                id_to_files[prefix]['swir'][file_type] = fpath


# Note: here are the AOI boundaries for the current AOIs
# D1: 747285 747908 4407065 4407640
# D2: 749352 750082 4407021 4407863
# D3: 477268 478256 3637333 3638307
# D4: 435532 436917 3354107 3355520


def main(args):
    parser = argparse.ArgumentParser(
        description="Run the Danesfield processing pipeline on an AOI from start to finish.")
    parser.add_argument("ini_file",
                        help="ini file")
    args = parser.parse_args(args)

    # Read configuration file
    config = configparser.ConfigParser()
    config.read(args.ini_file)

    # This either parses the working directory from the configuration file and passes it to
    # create the working directory or passes None and so some default working directory is
    # created (based on the time of creation)
    working_dir = create_working_dir(config['paths'].get('work_dir'),
                                     config['paths']['imagery_dir'])

    aoi_name = config['aoi']['name']

    gsd = float(config['params'].get('gsd', 0.25))

    #############################################
    # Run P3D point cloud generation
    #############################################
    # TODO implement running P3D from Docker
    p3d_file = config['paths']['p3d_fpath']

    #############################################
    # Find all NTF and corresponding info tar
    # files
    #############################################
    ntf_fpaths = []
    info_fpaths = []
    for root, dirs, files in os.walk(config['paths']['imagery_dir']):
        ntf_fpaths.extend([os.path.join(root, file)
                           for file in files if file.lower().endswith('.ntf')])
        info_fpaths.extend([os.path.join(root, file)
                            for file in files if file.lower().endswith('.tar')])

    # We look for the rpc files in a different dir
    rpc_fpaths = []
    for root, dirs, files in os.walk(config['paths'].get('rpc_dir')):
        rpc_fpaths.extend([os.path.join(root, file)
                           for file in files
                           if file.lower().endswith('.rpc') and
                           file.lower().startswith('gra_')])

    # We start with prefixes as a set so that we're only adding the unique ones.
    prefixes = set()
    prefix_regex = re.compile('[0-9]{2}[A-Z]{3}[0-9]{8}-')
    for ntf_fpath in ntf_fpaths:
        prefix = prefix_regex.search(ntf_fpath)
        if prefix:
            prefixes.add(prefix.group(0).rstrip('-'))
    prefixes = list(prefixes)

    # Group the modalities with the collection data prefix
    collection_id_to_files = {}
    incomplete_ids = []
    for prefix in prefixes:
        collection_id_to_files[prefix] = {
            'pan': {},
            'msi': {},
            'swir': {},
            'angle': -1,
        }

        classify_fpaths(prefix, ntf_fpaths, collection_id_to_files, 'image')
        classify_fpaths(prefix, rpc_fpaths, collection_id_to_files, 'rpc')
        classify_fpaths(prefix, info_fpaths, collection_id_to_files, 'info')

        # If we didn't pick up all of the modalities, then delete the entry from the dictionary.
        # For now, we aren't running the check on SWIR.
        complete = (ensure_complete_modality(collection_id_to_files[prefix]['pan'],
                                             require_rpc=True)
                    and ensure_complete_modality(collection_id_to_files[prefix]['msi'],
                                                 require_rpc=True))

        if not complete:
            logging.warning("Don't have complete modality for collection ID: '{}', skipping!"
                            .format(prefix))
            del collection_id_to_files[prefix]
            incomplete_ids.append(prefix)

    #############################################
    # Render DSM from P3D point cloud
    #############################################

    dsm_file = os.path.join(working_dir, aoi_name + '_P3D_DSM.tif')
    cmd_args = [dsm_file, '-s', p3d_file]
    cmd_args += ['--gsd', str(gsd)]

    bounds = config['aoi'].get('bounds')
    if bounds:
        cmd_args += ['--bounds']
        cmd_args += bounds.split(' ')

    logging.info("---- Running generate_dsm.py ----")
    logging.debug(cmd_args)
    generate_dsm.main(cmd_args)

    # #############################################
    # # Fit Dtm to the DSM
    # #############################################

    dtm_file = os.path.join(working_dir, aoi_name + '_DTM.tif')
    cmd_args = [dsm_file, dtm_file]
    logging.info("---- Running fit_dtm.py ----")
    logging.debug(cmd_args)
    fit_dtm.main(cmd_args)

    #############################################
    # Orthorectify images
    #############################################

    # For each MSI source image call orthorectify.py
    # needs to use the DSM, DTM from above and Raytheon RPC file,
    # which is a by-product of P3D.
    for collection_id, files in collection_id_to_files.items():
        # Orthorectify the msi images
        msi_ntf_fpath = files['msi']['image']
        msi_fname = os.path.splitext(os.path.split(msi_ntf_fpath)[1])[0]
        msi_ortho_img_fpath = os.path.join(working_dir, '{}_ortho.tif'.format(msi_fname))
        cmd_args = [msi_ntf_fpath, dsm_file, msi_ortho_img_fpath, '--dtm', dtm_file]

        msi_rpc_fpath = files['msi'].get('rpc', None)
        if msi_rpc_fpath:
            cmd_args.extend(['--raytheon-rpc', msi_rpc_fpath])

        orthorectify.main(cmd_args)
        files['msi']['ortho_img_fpath'] = msi_ortho_img_fpath
    #
    # Note: we may eventually select a subset of input images
    # on which to run this and the following steps

    #############################################
    # Compute NDVI
    #############################################
    # Compute the NDVI from the orthorectified / pansharpened images
    # for use during segmentation
    logging.info('---- Compute NDVI ----')
    ndvi_output_fpath = os.path.join(working_dir, 'ndvi.tif')
    cmd_args = [files['msi']['ortho_img_fpath'] for
                files in
                collection_id_to_files.values() if
                'msi' in files and 'ortho_img_fpath' in files['msi']]
    cmd_args.append(ndvi_output_fpath)
    script_call = ["compute_ndvi.py"] + cmd_args
    print(*script_call)
    compute_ndvi.main(cmd_args)

    #############################################
    # Get OSM road vector data
    #############################################
    # Query OpenStreetMap for road vector data
    logging.info('---- Fetching OSM road vector ----')
    road_vector_output_fpath = os.path.join(working_dir, 'road_vector.geojson')
    cmd_args = ['--bounding-img', dsm_file,
                '--output-dir', working_dir]
    script_call = ["get_road_vector.py"] + cmd_args
    print(*script_call)
    get_road_vector.main(cmd_args)

    #############################################
    # Segment by Height and Vegetation
    #############################################
    # Call segment_by_height.py using the DSM, DTM, and NDVI.  the
    # output here has the suffix _threshold_CLS.tif.
    logging.info('---- Segmenting by Height and Vegetation ----')
    threshold_output_mask_fpath = os.path.join(working_dir, 'threshold_CLS.tif')
    cmd_args = [dsm_file,
                dtm_file,
                threshold_output_mask_fpath,
                '--input-ndvi', ndvi_output_fpath]
    cmd_args.extend(['--road-vector',
                     road_vector_output_fpath,
                     '--road-rasterized',
                     os.path.join(working_dir, 'road_rasterized.tif'),
                     '--road-rasterized-bridge',
                     os.path.join(working_dir, 'road_rasterized_bridge.tif')])
    segment_by_height.main(cmd_args)

    #############################################
    # Material Segmentation
    #############################################
    logging.info('---- Running material segmentation classifier ----')
    cmd_args = ['--image_paths']
    # We build these up separately because they have to be 1-to-1 on the command line and
    # dictionaries are unordered
    img_paths = []
    info_paths = []
    for collection_id, files in collection_id_to_files.items():
        img_paths.append(files['msi']['ortho_img_fpath'])
        info_paths.append(files['msi']['info'])
    cmd_args.extend(img_paths)
    cmd_args.append('--info_paths')
    cmd_args.extend(info_paths)
    cmd_args.extend(['--output_dir', working_dir,
                     '--model_path', config['material']['model_fpath'],
                     '--outfile_prefix', aoi_name])
    if config.has_option('material', 'batch_size'):
        cmd_args.extend(['--batch_size', config.get('material', 'batch_size')])
    if config['material'].getboolean('cuda'):
            cmd_args.append('--cuda')
    logging.info(cmd_args)
    material_classifier.main(cmd_args)

    #############################################
    # Roof Geon Extraction & PointNet Geon Extraction
    #############################################
    # This script encapsulates both Columbia's and Purdue's components
    # for roof segmentation and geon extraction / reconstruction
    # Output files are named building_<N>.obj and building_<N>.json where <N> is
    # a integer, starting at 0.
    logging.info('---- Running roof geon extraction ----')
    cmd_args = [
        '--las', p3d_file,
        # Note that we're currently using the CLS file from the
        # segment by height script
        '--cls', threshold_output_mask_fpath,
        '--dtm', dtm_file,
        '--model_dir', config['roof']['model_dir'],
        '--model_prefix', config['roof']['model_prefix'],
        '--output_dir', working_dir
    ]
    logging.info(cmd_args)
    roof_geon_extraction.main(cmd_args)

    #############################################
    # Texture Mapping
    #############################################
    logging.info("---- Preparing data for texture mapping ----")
    for collection_id, files in collection_id_to_files.items():
        cmd_args = [dsm_file, working_dir, "--pan", files['pan']['image']]
        rpc_fpath = files['pan'].get('rpc', None)
        if (rpc_fpath):
            cmd_args.append(rpc_fpath)
        cmd_args.extend(["--msi", files['msi']['image']])
        rpc_fpath = files['msi'].get('rpc', None)
        if (rpc_fpath):
            cmd_args.append(rpc_fpath)
        script_call = ["crop_and_pansharpen.py"] + cmd_args
        print(*script_call)
        crop_and_pansharpen.main(cmd_args)

    occlusion_mesh = "xxxx.obj"
    images_to_use = glob.glob(os.path.join(working_dir, "*_crop_pansharpened_processed.tif"))
    orig_meshes = glob.glob(os.path.join(working_dir, "*.obj"))
    orig_meshes = [e for e in orig_meshes
                   if e.find(occlusion_mesh) < 0 and e.find("building_") < 0]
    cmd_args = [dsm_file, dtm_file, working_dir, occlusion_mesh, "--crops"]
    cmd_args.extend(images_to_use)
    cmd_args.append("--buildings")
    cmd_args.extend(orig_meshes)
    script_call = ["texture_mapping.py"] + cmd_args
    print(*script_call)
    texture_mapping.main(cmd_args)

    #############################################
    # Buildings to DSM
    #############################################
    logging.info('---- Running buildings to dsm ----')

    # Generate the output DSM
    output_dsm = os.path.join(working_dir, "buildings_to_dsm_DSM.tif")
    cmd_args = [
        dtm_file,
        output_dsm]
    cmd_args.append('--input_obj_paths')
    obj_list = glob.glob("{}/*.obj".format(working_dir))
    # remove occlusion_mesh and results (building_<i>.obj)
    obj_list = [e for e in obj_list
                if e.find(occlusion_mesh) < 0 and e.find("building_") < 0]
    cmd_args.extend(obj_list)
    logging.info(cmd_args)
    buildings_to_dsm.main(cmd_args)

    # Generate the output CLS
    output_cls = os.path.join(working_dir, "buildings_to_dsm_CLS.tif")
    cmd_args = [
        dtm_file,
        output_cls,
        '--render_cls']
    cmd_args.append('--input_obj_paths')
    cmd_args.extend(obj_list)
    script_call = ["buildings_to_dsm.py"] + cmd_args
    print(*script_call)
    buildings_to_dsm.main(cmd_args)

    #############################################
    # Run metrics
    #############################################
    logging.info('---- Running scoring code ----')

    # Expected file path for material classification output MTL file
    output_mtl = os.path.join(working_dir, '{}_MTL.tif'.format(aoi_name))

    run_metrics_output_dir = os.path.join(working_dir, "metrics")
    cmd_args = [
        '--output-dir', run_metrics_output_dir,
        '--ref-dir', config['metrics']['ref_data_dir'],
        '--ref-prefix', config['metrics']['ref_data_prefix'],
        '--dsm', output_dsm,
        '--cls', output_cls,
        '--mtl', output_mtl,
        '--dtm', dtm_file]
    script_call = ["run_metrics.py"] + cmd_args
    print(*script_call)
    run_metrics.main(cmd_args)


if __name__ == '__main__':
    loglevel = os.environ.get('LOGLEVEL', 'INFO').upper()
    logging.basicConfig(level=loglevel)

    main(sys.argv[1:])
