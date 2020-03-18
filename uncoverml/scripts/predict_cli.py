"""
Run the uncoverml pipeline for clustering, supervised learning and prediction.

.. program-output:: uncoverml --help
"""
import logging
import pickle
import resource
from os.path import isfile, splitext, exists
import os
import shutil
import warnings

import click
import numpy as np
import matplotlib
matplotlib.use('Agg')

import uncoverml as ls
import uncoverml.cluster
import uncoverml.config
import uncoverml.features
import uncoverml.geoio
import uncoverml.learn
import uncoverml.mllog
import uncoverml.mpiops
import uncoverml.predict
import uncoverml.validate
import uncoverml.targets
import uncoverml.models
from uncoverml.transforms import StandardiseTransform
from uncoverml.learn import all_modelmaps


_logger = logging.getLogger(__name__)
# warnings.showwarning = warn_with_traceback
warnings.filterwarnings(action='ignore', category=FutureWarning)
warnings.filterwarnings(action='ignore', category=DeprecationWarning)

def _algo_for_model(model, config):
    """
    Work out the algorithm for the model we have.
    """
    for k, v in all_modelmaps.items():
        if isinstance(model, v):
            config.algorithm = k
            config.set_algo_flags()
            _logger.info("Model algorithm detected as '%s'", k)
            return
    raise NameError("Model algorithm not recognised by uncoverml")

def main(config_file, partitions, mask, retain):
    config = ls.config.Config(config_file, predicting=True)
    model = _load_model(config)
    # If we have a list of models, assume it's a bootstrap 
    #  ensemble. If we have collections of models for different 
    #  reasons in future, will have to add specific config args.
    bootstrapping = isinstance(model, list)
    if bootstrapping:
        _algo_for_model(model[0], config)
    else:
        _algo_for_model(model, config)

    if bootstrapping and config.bootstrap_predictions is None:
        config.bootstrap_predictions = len(model)
    elif bootstrapping and config.bootstrap_predictions > len(model):
        config.bootstrap_predictions = len(model)
        _logger.warning("Number of predictions to perform (set by prediction 'bootstrap' "
                        "parameter is less than number of bootstrapped models available "
                        f"({len(model)}). Running predictions on all available models.")
    elif not bootstrapping and config.bootstrap_predictions:
        _logger.warning("'bootstrap' was provided as part of prediction configuration, but a "
                        "non-enesemble/bootstrapped model was provided. Running a single "
                        "prediction instead.")

    if config.extents:
        ls.geoio.crop_covariates(config)

    config.mask = mask if mask else config.mask
    if config.mask:
        config.retain = retain if retain else config.retain

        if not isfile(config.mask):
            config.mask = ''
            _logger.info("A mask was provided, but the file does not exist on "
                         "disc or is not a file.")

    config.n_subchunks = partitions
    if config.n_subchunks > 1:
        _logger.info("Memory contstraint forcing {} iterations "
                     "through data".format(config.n_subchunks))
    else:
        _logger.info("Using memory aggressively: dividing all data between nodes")

    if bootstrapping:
        image_shape, image_bbox, image_crs = ls.geoio.get_image_spec(model[0], config)
    else:
        image_shape, image_bbox, image_crs = ls.geoio.get_image_spec(model, config)

    if bootstrapping:
        predict_tags = ['Prediction', 'Variance', 'Lower quantile', 'Upper quantile']
    else:
        predict_tags = model.get_predict_tags()

    image_out = ls.geoio.ImageWriter(image_shape, image_bbox, image_crs,
                                     config.n_subchunks, config.prediction_file, config.outbands,
                                     band_tags=predict_tags, **config.geotif_options)

    print(f"subchunks: {config.n_subchunks}")
    for i in range(config.n_subchunks):
        _logger.info("starting to render partition {}".format(i+1))
        ls.predict.render_partition(
            model, i, image_out, config, bootstrapping=bootstrapping, 
            bs_predictions=config.bootstrap_predictions)

    image_out.close()

    if config.clustering and config.cluster_analysis:
        if ls.mpiops.chunk_index == 0:
            ls.predict.final_cluster_analysis(config.n_classes,
                                              config.n_subchunks)

    if config.thumbnails:
        image_out.output_thumbnails(config.thumbnails)

    ls.mpiops.run_once(
        write_prediction_metadata,
        model, config, config.metadata_file)

    if config.extents:
        ls.mpiops.run_once(_clean_temp_cropfiles, config)

    _logger.info("Finished! Total mem = {:.1f} GB".format(_total_gb()))

def write_prediction_metadata(model, config, out_filename="metadata.txt"):
    """
    write the metadata for this prediction result, into a human-readable txt file.
    in order to make the ML results traceable and reproduceable (provenance)
    :return:
    """
    from uncoverml.metadata_profiler import MetadataSummary

    mobj = MetadataSummary(model, config)
    mobj.write_metadata(out_filename)

    return out_filename

def _total_gb():
    # given in KB so convert
    my_usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2)
    # total_usage = mpiops.comm.reduce(my_usage, root=0)
    total_usage = ls.mpiops.comm.allreduce(my_usage)
    return total_usage

def _load_model(config):
    with open(config.model_file, 'rb') as f:
        return pickle.load(f)

def _clean_temp_cropfiles(config):
    shutil.rmtree(config.tmpdir)   

