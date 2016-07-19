from __future__ import division
from abc import ABCMeta, abstractmethod

import rasterio
import os.path
import numpy as np
from affine import Affine
import shapefile
import tables as hdf
import logging
import time

log = logging.getLogger(__name__)


def file_indices_okay(filenames):

    # get just the name eg /path/to/file_0.hdf5 -> file_0
    basenames = [os.path.splitext(os.path.basename(k))[0] for k in filenames]

    # file.part0of3 -> [file,0]
    split_total = [k.rsplit('of', 1) for k in basenames]
    try:
        totals = set([int(k[1]) for k in split_total])
    except:
        log.error("Filename does not contain total number of parts.")
        return False

    if len(totals) > 1:
        log.error("Files disagree about total chunks")
        return False

    total = totals.pop()

    minus_total = [k[0] for k in split_total]
    base_and_idx = [k.rsplit('.part', 1) for k in minus_total]
    bases = set([k[0] for k in base_and_idx])
    log.info("Input file sets: {}".format(set(bases)))

    # check every base has the right indices
    # "[[file,0], [file,1]] -> {file:[0,1]}
    try:
        base_ids = {k: set([int(j[1])
                           for j in base_and_idx if j[0] == k]) for k in bases}
    except:
        log.error("One or more filenames are not in <name>.part<idx>of<total>"
                  ".hdf5 format")
        # either there are no ints at the end or no underscore
        return False

    # Ensure all files are present
    true_set = set(range(1, total + 1))
    files_ok = True
    for b, nums in base_ids.items():
        if not nums == true_set:
            files_ok = False
            log.error("feature {} has wrong files. ".format(b))
            missing = true_set.difference(nums)
            if len(missing) > 0:
                log.error("Missing Index: {}".format(missing))
            extra = nums.difference(true_set)
            if len(extra) > 0:
                log.error("Extra Index: {}".format(extra))
    return files_ok


def files_by_chunk(filenames):
    """
    returns a dictionary per-chunk of a *sorted* list of features
    Note: assumes files_indices_ok returned true
    """

    # get just the name eg /path/to/file.part0.hdf5 -> file.part0
    def transform(x):
        return os.path.splitext(os.path.basename(x))[0]
    sorted_filenames = sorted(filenames, key=transform)
    basenames = [transform(k) for k in sorted_filenames]

    split_total = [k.rsplit('of', 1) for k in basenames]
    minus_total = [k[0] for k in split_total]
    indices = [int(k.rsplit('.part', 1)[1]) - 1 for k in minus_total]

    d = {i: [] for i in set(indices)}
    for i, f in zip(indices, sorted_filenames):
        d[i].append(f)
    return d



def points_from_hdf(filename, fieldnames):
    """
    TODO
    """
    vals = {}
    with hdf.open_file(filename, mode='r') as f:
        for fld in fieldnames:
            vals[fld] = (f.get_node("/" + fld).read())

    return vals


def points_to_hdf(outfile, fielddict={}):

    with hdf.open_file(outfile, 'w') as f:
        for fld, v in fielddict.items():
            f.create_array("/", fld, obj=v)


def construct_splits(npixels, nchunks, overlap=0):
    # Build the equivalent windowed image
    # y bounds are EXCLUSIVE
    y_arrays = np.array_split(np.arange(npixels), nchunks)
    y_bounds = []
    # construct the overlap
    for i, s in enumerate(y_arrays):
        if i == 0:
            p_min = s[0]
            p_max = s[-1] + overlap + 1
        elif i == len(y_arrays) - 1:
            p_min = s[0] - overlap
            p_max = s[-1] + 1
        else:
            p_min = s[0] - overlap
            p_max = s[-1] + overlap + 1
        y_bounds.append((p_min, p_max))
    return y_bounds


class ImageSource(metaclass=ABCMeta):

    @abstractmethod
    def data(self, min_x, max_x, min_y, max_y):
        pass

    @property
    def full_resolution(self):
        return self._full_res

    @property
    def dtype(self):
        return self._dtype

    @property
    def nodata_value(self):
        return self._nodata_value

    @property
    def pixsize_x(self):
        return self._pixsize_x

    @property
    def pixsize_y(self):
        return self._pixsize_y

    @property
    def origin_latitude(self):
        return self._start_lat

    @property
    def origin_longitude(self):
        return self._start_lon


class RasterioImageSource(ImageSource):

    def __init__(self, filename):

        self._filename = filename
        with rasterio.open(self._filename, 'r') as geotiff:
            self._full_res = (geotiff.width, geotiff.height, geotiff.count)
            self._nodata_value = geotiff.meta['nodata']
            # we don't support different channels with different dtypes
            for d in geotiff.dtypes[1:]:
                if geotiff.dtypes[0] != d:
                    raise ValueError("No support for multichannel geotiffs "
                                     "with differently typed channels")
            self._dtype = np.dtype(geotiff.dtypes[0])

            A = geotiff.affine
            # No shearing or rotation allowed!!
            if not ((A[1] == 0) and (A[3] == 0)):
                raise RuntimeError("Transform to pixel coordinates"
                                   "has rotation or shear")
            self._pixsize_x = A[0]
            self._pixsize_y = A[4]
            self._start_lon = A[2]
            self._start_lat = A[5]

    def data(self, min_x, max_x, min_y, max_y):
        # ((ymin, ymax),(xmin, xmax))
        # NOTE these are exclusive
        window = ((min_y, max_y), (min_x, max_x))
        with rasterio.open(self._filename, 'r') as geotiff:
            d = geotiff.read(window=window, masked=True)
        d = d[np.newaxis, :, :] if d.ndim == 2 else d
        d = np.ma.transpose(d, [2, 1, 0])  # Transpose and channels at back

        # uniform mask format
        if np.ma.count_masked(d) == 0:
            d = np.ma.masked_array(data=d.data,
                                   mask=np.zeros_like(d.data, dtype=bool))
        return d


class ArrayImageSource(ImageSource):
    """
    An image source that uses an internally stored numpy array

    Parameters
    ----------
    A : MaskedArray
        masked array of shape (xpix, ypix, channels) that contains the
        image data.
    origin : ndarray
        Array of the form [lonmin, latmin] that defines the origin of the image
    pixsize : ndarray
        Array of the form [pixsize_x, pixsize_y] defining the size of a pixel
    """
    def __init__(self, A, origin, pixsize):
        self._data = A
        self._full_res = A.shape
        self._dtype = A.dtype
        self._nodata_value = A.fill_value
        self._pixsize_x = pixsize[0]
        self._pixsize_y = pixsize[1]
        self._start_lon = origin[0]
        self._start_lat = origin[1]

    def data(self, min_x, max_x, min_y, max_y):
        # MUST BE EXCLUSIVE
        data_window = self._data[min_x:max_x, :][:, min_y:max_y]
        return data_window


class Image:
    def __init__(self, source, chunk_idx=0, nchunks=1, overlap=0):
        assert chunk_idx >= 0 and chunk_idx < nchunks

        if nchunks == 1 and overlap != 0:
            log.warn("Ignoring overlap when 1 chunk present")
            overlap = 0

        self.chunk_idx = chunk_idx
        self.nchunks = nchunks
        self.source = source

        log.info("Image has resolution {}".format(source.full_resolution))
        log.info("Image has datatype {}".format(source.dtype))
        log.info("Image missing value: {}".format(source.nodata_value))

        self._full_res = source.full_resolution
        self._start_lon = source.origin_longitude
        self._start_lat = source.origin_latitude
        self.pixsize_x = source.pixsize_x
        self.pixsize_y = source.pixsize_y
        self._y_flipped = source.pixsize_y < 0

        # construct the canonical pixel<->position map
        pix_x = range(self._full_res[0] + 1)  # outer corner of last pixel
        coords_x = [self._start_lon + float(k) * self.pixsize_x
                    for k in pix_x]
        self._coords_x = coords_x
        pix_y = range(self._full_res[1] + 1)  # ditto
        coords_y = [self._start_lat + float(k) * self.pixsize_y
                    for k in pix_y]
        self._coords_y = coords_y
        self._pix_x_to_coords = dict(zip(pix_x, coords_x))
        self._pix_y_to_coords = dict(zip(pix_y, coords_y))

        # exclusive y range of this chunk in full image
        ymin, ymax = construct_splits(self._full_res[1],
                                      nchunks, overlap)[chunk_idx]
        self._offset = np.array([0, ymin], dtype=int)
        # exclusive x range of this chunk (same for all chunks)
        xmin, xmax = 0, self._full_res[0]

        assert(xmin < xmax)
        assert(ymin < ymax)

        # get resolution of this chunk
        xres = self._full_res[0]
        yres = ymax - ymin

        # Calculate the new values for resolution and bounding box
        self.resolution = (xres, yres, self._full_res[2])

        start_bound_x, start_bound_y = self._global_pix2lonlat(
            np.array([[xmin, ymin]]))[0]
        # one past the last pixel
        outer_bound_x, outer_bound_y = self._global_pix2lonlat(
            np.array([[xmax, ymax]]))[0]
        assert(start_bound_x < outer_bound_x)
        if self._y_flipped:
            assert(start_bound_y > outer_bound_y)
            self.bbox = [[start_bound_x, outer_bound_x],
                         [outer_bound_y, start_bound_y]]
        else:
            assert(start_bound_y < outer_bound_y)
            self.bbox = [[start_bound_x, outer_bound_x],
                         [start_bound_y, outer_bound_y]]

    def __repr__(self):
        return "<geo.Image({}), chunk {} of {})>".format(self.source,
                                                         self.chunk_idx,
                                                         self.nchunks)

    def data(self):
        xmin = self._offset[0]
        xmax = self._offset[0] + self.resolution[0]
        ymin = self._offset[1]
        ymax = self._offset[1] + self.resolution[1]
        data = self.source.data(xmin, xmax, ymin, ymax)
        return data

    @property
    def nodata_value(self):
        return self.source.nodata_value

    @property
    def dtype(self):
        return self.source.dtype

    @property
    def xres(self):
        return self.resolution[0]

    @property
    def yres(self):
        return self.resolution[1]

    @property
    def channels(self):
        return self.resolution[2]

    @property
    def npoints(self):
        return self.resolution[0] * self.resolution[1]

    @property
    def x_range(self):
        return self.bbox[0]

    @property
    def y_range(self):
        return self.bbox[1]

    @property
    def xmin(self):
        return self.bbox[0][0]

    @property
    def xmax(self):
        return self.bbox[0][1]

    @property
    def ymin(self):
        return self.bbox[1][0]

    @property
    def ymax(self):
        return self.bbox[1][1]

    def patched_shape(self, patchsize):
        eff_shape = (self.xres - 2 * patchsize,
                     self.yres - 2 * patchsize)
        return eff_shape

    def patched_bbox(self, patchsize):
        start = [patchsize, patchsize]
        end_p1 = [self.xres - patchsize + 1,  # +1 because bbox
                  self.yres - patchsize + 1]  # +1 because bbox
        xy = np.array([start, end_p1])
        eff_bbox = self.pix2lonlat(xy)
        return eff_bbox

    # @contract(xy='array[Nx2](int64),N>0')
    def _global_pix2lonlat(self, xy):
        result = np.array([[self._pix_x_to_coords[x],
                           self._pix_y_to_coords[y]] for x, y in xy])
        return result

    # @contract(xy='array[Nx2](int64),N>0')
    def pix2lonlat(self, xy):
        result = self._global_pix2lonlat(xy + self._offset)
        return result

    # @contract(lonlat='array[Nx2](float64),N>0')
    def _global_lonlat2pix(self, lonlat):
        x = np.searchsorted(self._coords_x, lonlat[:, 0], side='right') - 1
        x = x.astype(int)
        # searchsorted only works for increasing arrays
        ycoords = self._coords_y[::-1] if self._y_flipped else self._coords_y
        # side = 'left' if self._y_flipped else 'right'
        if self._y_flipped:
            y = np.searchsorted(ycoords, lonlat[:, 1], side='left')
        else:
            y = np.searchsorted(ycoords, lonlat[:, 1], side='right') - 1

        y = self._full_res[1] - y if self._y_flipped else y
        y = y.astype(int)

        # We want the *closed* interval, which means moving
        # points on the end back by 1
        on_end_x = lonlat[:, 0] == self._coords_x[-1]
        on_end_y = lonlat[:, 1] == self._coords_y[-1]
        x[on_end_x] -= 1
        y[on_end_y] -= 1
        if (not all(np.logical_and(x >= 0, x < self._full_res[0]))) or \
                (not all(np.logical_and(y >= 0, y < self._full_res[1]))):
            import IPython; IPython.embed(); import sys; sys.exit()
            raise ValueError("Queried location is not in the image!")

        result = np.concatenate((x[:, np.newaxis], y[:, np.newaxis]), axis=1)
        return result

    # @contract(lonlat='array[Nx2](float64),N>0')
    def lonlat2pix(self, lonlat):
        result = self._global_lonlat2pix(lonlat) - self._offset
        # check the postcondition
        x = result[:, 0]
        y = result[:, 1]

        if (not all(np.logical_and(x >= 0, x < self.resolution[0]))) or \
                (not all(np.logical_and(y >= 0, y < self.resolution[1]))):
            raise ValueError("Queried location is not in the image!")

        return result

    def in_bounds(self, lonlat):
        xy = self._global_lonlat2pix(lonlat)
        xy -= self._offset
        x = xy[:, 0]
        y = xy[:, 1]
        inx = np.logical_and(x >= 0, x < self.resolution[0])
        iny = np.logical_and(y >= 0, y < self.resolution[1])
        result = np.logical_and(inx, iny)
        return result


def bbox2affine(xmax, xmin, ymax, ymin, xres, yres):

    pixsize_x = (xmax - xmin) / (xres + 1)
    pixsize_y = (ymax - ymin) / (yres + 1)

    A = Affine(pixsize_x, 0, xmin,
               0, -pixsize_y, ymax)

    return A, pixsize_x, pixsize_y


def output_filename(feature_name, chunk_index, n_chunks, output_dir):
    filename = feature_name + ".part{}of{}.hdf5".format(chunk_index + 1,
                                                        n_chunks)
    full_path = os.path.join(output_dir, filename)
    return full_path


def output_blank(filename, shape=None, bbox=None):
    with hdf.open_file(filename, mode='w') as h5file:
        h5file.root._v_attrs["blank"] = True
        if shape is not None:
            h5file.root._v_attrs["image_shape"] = shape
        if bbox is not None:
            h5file.root._v_attrs["image_bbox"] = bbox


def output_features(feature_vector, outfile, featname="features",
                    shape=None, bbox=None):
    """
    Writes a vector of features out to a standard HDF5 format. The function
    assumes that it is only 1 chunk of a larger vector, so outputs a numerical
    suffix to the file as an index.

    Parameters
    ----------
        feature_vector: array
            A 2D numpy array of shape (nPoints, nDims) of type float. This can
            be a masked array.
        outfile: path
            The name of the output file
        featname: str, optional
            The name of the features.
        shape: tuple, optional
            The original shape of the feature for reproducing an image
        bbox: ndarray, optional
            The bounding box of the original data for reproducing an image
    """
    with hdf.open_file(outfile, mode='w') as h5file:
        h5file.root._v_attrs["blank"] = False

        # Make sure we are writing "long" arrays
        if feature_vector.ndim < 2:
            feature_vector = feature_vector[:, np.newaxis]
        array_shape = feature_vector.shape

        filters = hdf.Filters(complevel=5, complib='zlib')

        if np.ma.isMaskedArray(feature_vector):
            fobj = feature_vector.data
            if np.ma.count_masked(feature_vector) == 0:
                fmask = np.zeros(array_shape, dtype=bool)
            else:
                fmask = feature_vector.mask
        else:
            fobj = feature_vector
            fmask = np.zeros(array_shape, dtype=bool)

        h5file.create_carray("/", featname, filters=filters,
                             atom=hdf.Float64Atom(), shape=array_shape,
                             obj=fobj)

        h5file.create_carray("/", "mask", filters=filters,
                             atom=hdf.BoolAtom(), shape=array_shape, obj=fmask)

        if shape is not None:
            h5file.root._v_attrs["image_shape"] = shape
        if bbox is not None:
            h5file.root._v_attrs["image_bbox"] = bbox

    start = time.time()
    file_exists = False

    while not file_exists and (time.time() - start) < 5:

        file_exists = os.path.exists(outfile)
        time.sleep(0.1)

    if not file_exists:
        raise RuntimeError("{} never written!".format(outfile))

    return True


def load_and_cat(hdf5_vectors):
    data_shapes = []
    # pass one to get the shapes
    for filename in hdf5_vectors:
        with hdf.open_file(filename, mode='r') as f:
            if f.root._v_attrs["blank"]:  # no data in this chunk
                return None
            data_shapes.append(f.root.features.shape)

    # allocate memory
    x_shps, y_shps = zip(*data_shapes)
    x_shp = set(x_shps).pop()
    y_shp = np.sum(np.array(y_shps))

    log.info("Allocating shape {}, mem {}".format((x_shp, y_shp),
                                                  x_shp * y_shp * 72. / 1e9))

    all_data = np.empty((x_shp, y_shp), dtype=float)
    all_mask = np.empty((x_shp, y_shp), dtype=bool)

    # read files in
    start_idx = 0
    end_idx = -1
    for filename in hdf5_vectors:
        with hdf.open_file(filename, mode='r') as f:
            end_idx = start_idx + f.root.features.shape[1]
            all_data[:, start_idx:end_idx] = f.root.features[:]
            all_mask[:, start_idx:end_idx] = f.root.mask[:]
            start_idx = end_idx

    result = np.ma.masked_array(data=all_data, mask=all_mask)
    return result


def load_attributes(filename_dict):
    # Only bother loading the first one as they're all the same for now
    fname = filename_dict[0][0]
    shape = None
    bbox = None
    with hdf.open_file(fname, mode='r') as f:
        if 'image_shape' in f.root._v_attrs:
            shape = f.root._v_attrs.image_shape
        if 'image_bbox' in f.root._v_attrs:
            bbox = f.root._v_attrs.image_bbox
    return shape, bbox


def load_shapefile(filename, field):
    """
    TODO
    """

    sf = shapefile.Reader(filename)
    fdict = {f[0]: i for i, f in enumerate(sf.fields[1:])}  # Skip DeletionFlag

    if field not in fdict:
        raise ValueError("Requested field is not in records!")

    vind = fdict[field]
    vals = np.array([r[vind] for r in sf.records()])
    coords = []
    for shape in sf.iterShapes():
        coords.append(list(shape.__geo_interface__['coordinates']))
    label_coords = np.array(coords)
    return label_coords, vals
