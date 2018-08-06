#!/usr/bin/env python

"""
Run the Danesfield processing pipeline on an AOI from start to finish.
"""

import configparser
import datetime
import logging
import os
import re
import subprocess
import sys

# import other tools
import gdal
import generate_dsm
import fit_dtm
import material_classifier
import msi_to_rgb
import orthorectify
import segment_by_height


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


def main(config_fpath):
    # Read configuration file
    config = configparser.ConfigParser()
    config.read(config_fpath)

    # This either parses the working directory from the configuration file and passes it to
    # create the working directory or passes None and so some default working directory is
    # created (based on the time of creation)
    working_dir = create_working_dir(config['paths'].get('work_dir'),
                                     config['paths']['imagery_dir'])

    aoi_name = config['aoi']['name']
    aoi_bounds = map(int, config['aoi']['bounds'].split(' '))

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
                           for file in files if file.lower().endswith('.rpc')])

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
            'pansharpened_fpath': '',
            'rgb_fpath': '',
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
    cmd_args = [dsm_file, '-s', p3d_file, '--bounds']
    cmd_args += list(map(str, aoi_bounds))
    cmd_args += ['--gsd', str(gsd)]
    logging.info("---- Running generate_dsm.py ----")
    logging.debug(cmd_args)
    generate_dsm.main(cmd_args)

    #############################################
    # Fit DTM to the DSM
    #############################################

    dtm_file = os.path.join(working_dir, aoi_name + '_DTM.tif')
    cmd_args = [dsm_file, dtm_file]
    logging.info("---- Running fit_dtm.py ----")
    logging.debug(cmd_args)
    fit_dtm.main(cmd_args)

    #############################################
    # Orthorectify images
    #############################################

    # For each source image (PAN and MSI) call orthorectify.py
    # needs to use the DSM, DTM from above and Raytheon RPC file,
    # which is a by-product of P3D.
    for collection_id, files in collection_id_to_files.items():
        # Orthorectify the pan images
        pan_ntf_fpath = files['pan']['image']
        pan_fname = os.path.splitext(os.path.split(pan_ntf_fpath)[1])[0]
        pan_ortho_img_fpath = os.path.join(working_dir, '{}_ortho.tif'.format(pan_fname))
        cmd_args = [pan_ntf_fpath, dsm_file, pan_ortho_img_fpath, '--dtm', dtm_file]

        pan_rpc_fpath = files['pan'].get('rpc', None)
        if pan_rpc_fpath:
            cmd_args.extend(['--raytheon-rpc', pan_rpc_fpath])

        orthorectify.main(cmd_args)
        files['pan']['ortho_img_fpath'] = pan_ortho_img_fpath

        # Orthorectify the msi images
        msi_ntf_fpath = files['msi']['image']
        msi_fname = os.path.splitext(os.path.split(pan_ntf_fpath)[1])[0]
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
    # Pansharpen images
    #############################################
    # Call gdal_pansharpen.py (from GDAL, not Danesfield) like this:
    #    gdal_pansharpen.py PAN_image MSI_image output_image
    # on each of the pairs of matching PAN and MSI orthorectified
    # images from the step above
    logging.info('---- Pansharpening {} image pairs ----'.format(
                 len(collection_id_to_files.keys())))
    lowest_angle = float('inf')
    most_nadir_collection_id = ''
    for collection_id, files in collection_id_to_files.items():
        ortho_pan_fpath = files['pan']['ortho_img_fpath']
        ortho_msi_fpath = files['msi']['ortho_img_fpath']
        logging.info('\t Running on pair ({}, {})'.format(ortho_pan_fpath, ortho_msi_fpath))
        pansharpened_output_image = os.path.join(working_dir,
                                                 '{}_ortho_pansharpened.tif'.format(collection_id))
        cmd_args = ['gdal_pansharpen.py', ortho_pan_fpath, ortho_msi_fpath,
                    pansharpened_output_image]
        subprocess.run(cmd_args)
        files['pansharpened_fpath'] = pansharpened_output_image
        angle = float(gdal.Open(files['pan']['image'],
                                gdal.GA_ReadOnly).GetMetadata()['NITF_CSEXRA_OBLIQUITY_ANGLE'])
        if angle < lowest_angle:
            lowest_angle = angle
            most_nadir_collection_id = collection_id

    #############################################
    # Convert to 8-bit RGB
    #############################################
    # call msi_to_rgb.py on each of the previous Pansharpened images
    # with the '-b' flag to make byte images
    logging.info('---- Convert pansharpened MSI images to RGB ----')
    for collection_id, files in collection_id_to_files.items():
        rgb_image_fpath = os.path.join(working_dir, '{}_rgb_byte_image.tif'.format(collection_id))
        msi_image_fpath = files['pansharpened_fpath']

        cmd_args = [msi_image_fpath, rgb_image_fpath, '-b']
        msi_to_rgb.main(cmd_args)
        files['rgb_fpath'] = rgb_image_fpath

    #############################################
    # Segment by Height and Vegetation
    #############################################
    # Call segment_by_height.py using the DSM, DTM, and *one* of the
    # pansharpened images above.  We've been manually picking the most
    # nadir one.  We could do that here or generalize the code to average
    # or otherwise combine the NDVI map from multiple images.
    # the output here has the suffix _threshold_CLS.tif.
    logging.info('---- Segmenting by Height and Vegetation ----')
    # Choose the most NADIR image
    most_nadir_pan_fpath = collection_id_to_files[most_nadir_collection_id]['pansharpened_fpath']
    ndvi_output_fpath = os.path.join(working_dir, 'ndvi.tif')
    threshold_output_mask_fpath = os.path.join(working_dir, 'threshold_CLS.tif')
    cmd_args = [dsm_file, dtm_file, threshold_output_mask_fpath, '--msi', most_nadir_pan_fpath,
                '--ndvi', ndvi_output_fpath]
    segment_by_height.main(cmd_args)

    #############################################
    # UNet Semantic Segmentation
    #############################################

    # Collaborate with Chengjiang Long on what to run here

    #############################################
    # Columbia Building Segmentation
    #############################################

    # Run building_segmentation.py
    # Collaborate with Xu Zhang from Columbia University on how to run this

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
                     '--batch_size', str(config['material'].get('batch_size', 1024))])
    if config['material'].getboolean('cuda'):
            cmd_args.append('--cuda')
    logging.info(cmd_args)
    material_classifier.main(cmd_args)

    #############################################
    # PointNet Geon Extraction
    #############################################

    # Collaborate with Xu Zhang (at Columbia) on what to run here

    #############################################
    # Roof Geon Extraction
    #############################################

    # Collaborate with Zhixin Li (and others at Purdue) on what to run here
    # David Stoup is helping with conda packaging

    #############################################
    # Texture Mapping
    #############################################

    # Collaborate with Bastien Jacquet on what to run here
    # Dan Lipsa is helping with conda packaging


if __name__ == '__main__':
    loglevel = os.environ.get('LOGLEVEL', 'INFO').upper()
    logging.basicConfig(level=loglevel)

    main(sys.argv[1:])
