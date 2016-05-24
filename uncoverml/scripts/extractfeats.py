"""
Extract patch features from a single geotiff.

.. program-output:: extractfeats --help
"""
import logging
from functools import partial
import os
import click as cl
from uncoverml import geoio
import uncoverml.defaults as df
from uncoverml import parallel
import numpy as np
import pickle

log = logging.getLogger(__name__)


def extract_transform(data, x_sets):
    # Flatten patches
    data = data.reshape((data.shape[0], -1))
    if x_sets is not None:  # one-hot activated!
        data = parallel.one_hot(data, x_sets)
    data = data.astype(float)
    return data


def compute_unique_values(full_image, cluster):
    cluster.execute("x = feature.image_data_vector(data_dict)")
    x_sets = None
    # check data is okay
    dtype = full_image.dtype
    if dtype == np.dtype('float32') or dtype == np.dtype('float64'):
        log.warn("Cannot use one-hot for floating point data -- ignoring")
    else:
        cluster.execute("x_set = parallel.node_sets(x)")
        all_x_sets = cluster.pull('x_set')
        per_dim = zip(*all_x_sets)
        potential_x_sets = [np.unique(np.concatenate(k, axis=0))
                            for k in per_dim]
        total_dims = np.sum([len(k) for k in potential_x_sets])
        log.info("Total features from one-hot encoding: {}".format(
            total_dims))
        if total_dims <= df.max_onehot_dims:
            x_sets = potential_x_sets
        else:
            log.warn("Too many distinct values for one-hot encoding."
                     " If you're sure increase max value in default file.")
    return x_sets


@cl.command()
@cl.option('--quiet', is_flag=True, help="Log verbose output",
           default=df.quiet_logging)
@cl.option('--patchsize', type=int,
           default=df.feature_patch_size, help="window width of patches, i.e. "
           "patchsize of 0 is a single pixel, patchsize of 1 is a 3x3 patch, "
           "etc")
@cl.option('--chunks', type=int, default=df.work_chunks, help="Number of "
           "chunks in which to split the computation and output")
@cl.option('--targets', type=cl.Path(exists=True), help="Optional hdf5 file "
           "for providing target points at which to evaluate feature. See "
           "maketargets for creating an appropriate target files.")
@cl.option('--onehot', is_flag=True, help="Produce a one-hot encoding for "
           "each channel in the data. Ignored for float-valued data. "
           "Uses -0.5 and 0.5)")
@cl.option('--settings', type=cl.Path(exists=True), help="file containing"
           " previous setting used for evaluating testing data. If provided "
           "all other option flags are ignored")
@cl.option('--outputdir', type=cl.Path(exists=True), default=os.getcwd())
@cl.option('--ipyprofile', type=str, help="ipyparallel profile to use",
           default=None)
@cl.argument('name', type=str, required=True)
@cl.argument('geotiff', type=cl.Path(exists=True), required=True)
def main(name, geotiff, targets, onehot,
         chunks, patchsize, quiet, outputdir, ipyprofile, settings):
    """
    Extract patch features from a single geotiff and output to HDF5 file chunks
    for distribution to worker nodes.

    Apart from extracting patches from images, this also has the following
    functionality,

    - chunk the original geotiff into many small HDF5 files for distributed
      work
    - One-hot encode intger-valued/categorical layers
    - Only extract patches at specified locations given a target file
    """

    # setup logging
    if quiet is True:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    # build full filename for geotiff
    full_filename = os.path.abspath(geotiff)
    log.info("Input file full path: {}".format(full_filename))

    # Print some helpful statistics about the full image
    full_image = geoio.Image(full_filename)

    total_dims = full_image.resolution[2]
    log.info("Image has resolution {}".format(full_image.resolution))
    log.info("Image has datatype {}".format(full_image.dtype))
    log.info("Image missing value: {}".format(full_image.nodata_value))

    # Compute the effective sampled resolution accounting for patchsize
    eff_shape = None
    eff_bbox = None
    if targets is None:
        start = [patchsize, patchsize]
        end_p1 = [full_image.xres - patchsize + 1,  # +1 because bbox
                  full_image.yres - patchsize + 1]  # +1 because bbox
        xy = np.array([start, end_p1])
        eff_bbox = full_image.pix2latlon(xy, centres=False)
        eff_shape = (full_image.xres - 2 * patchsize,
                     full_image.yres - 2 * patchsize)
        log.info("Effective input resolution "
                 "after patch extraction: {}".format(eff_shape))
        log.info("Effective bounding box after "
                 "patch extraction: {}".format(eff_bbox))

    # build the chunk->image dictionary for the input data
    print(geotiff, chunks)
    image_dict = {i: geoio.Image(full_filename, i, chunks, patchsize)
                  for i in range(chunks)}

    # load settings
    f_args = {}
    if settings is not None:
        with open(settings, 'rb') as f:
            s = pickle.load(f)
            patchsize = s['cmd_args']['patchsize']
            log.info("Loading patchsize {} from settings file".format(
                patchsize))
            f_args.update(s['f_args'])

    # Initialise the cluster
    cluster = parallel.direct_view(ipyprofile, chunks)

    # Load the data into a dict on each client
    # Note chunk_indices is a global with different value on each node
    cluster.push({"image_dict": image_dict, "patchsize": patchsize,
                  "targets": targets})
    cluster.execute("data_dict = feature.load_image_data( "
                    "image_dict, chunk_indices, patchsize, targets)")

    # compute settings
    if settings is None:
        settings_dict = {}
        x_sets = compute_unique_values(full_image, cluster) if onehot else None
        f_args['x_sets'] = x_sets
        settings_filename = os.path.join(outputdir, name + "_settings.bin")
        settings_dict["f_args"] = f_args
        settings_dict["cmd_args"] = {'patchsize': patchsize}
        log.info("Writing feature settings to {}".format(settings_filename))
        with open(settings_filename, 'wb') as f:
            pickle.dump(settings_dict, f)

    # We have all the information we need, now build the transform
    log.info("Constructing feature transformation function")
    f = partial(extract_transform, **f_args)

    log.info("Applying transform across nodes")
    # Apply the transformation function
    cluster.push({"f": f, "featurename": name, "outputdir": outputdir,
                  "shape": eff_shape, "bbox": eff_bbox})
    log.info("Applying final transform and writing output files")
    cluster.execute("parallel.write_data(data_dict, f, featurename, "
                    "outputdir, shape=shape, bbox=bbox)")

    log.info("Output vector has length {}, dimensionality {}".format(
        full_image.resolution[0] * full_image.resolution[1], total_dims))
