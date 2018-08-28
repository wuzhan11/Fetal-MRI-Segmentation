import itertools
import os

import nibabel as nib
import numpy as np
import tables
from keras import Model
from tqdm import tqdm

from brats.utils import get_last_model_path
from fetal_net.utils.threaded_generator import ThreadedGenerator
from fetal_net.utils.utils import get_image, resize
from .training import load_old_model
from .utils import pickle_load
from .utils.patches import reconstruct_from_patches, get_patch_from_3d_data, compute_patch_indices, \
    get_set_of_patch_indices
from .augment import permute_data, generate_permutation_keys, reverse_permute_data


def get_set_of_patch_indices_full(start, stop, step):
    indices = []
    for start_i, stop_i, step_i in zip(start, stop, step):
        indices_i = list(range(start_i, stop_i + 1, step_i))
        if stop_i % step_i > 0:
            indices_i += [stop_i]
        indices += [indices_i]
    return np.array(list(itertools.product(*indices)))


def batch_iterator(indices, batch_size, data_0, patch_shape, truth_0, prev_truth_index, truth_patch_shape):
    i = 0
    while i < len(indices):
        batch = []
        curr_indices = []
        while len(batch) < batch_size and i < len(indices):
            curr_index = indices[i]
            patch = get_patch_from_3d_data(data_0, patch_shape=patch_shape, patch_index=curr_index)
            if truth_0 is not None:
                truth_index = list(curr_index[:2]) + [curr_index[2] + prev_truth_index]
                truth_patch = get_patch_from_3d_data(truth_0, patch_shape=truth_patch_shape,
                                                     patch_index=truth_index)
                patch = np.concatenate([patch, truth_patch], axis=-1)
            batch.append(patch)
            curr_indices.append(curr_index)
            i += 1
        yield [batch, curr_indices]
    print('Finished! {}-{}'.format(i, len(indices)))


def patch_wise_prediction(model: Model, data, patch_shape, overlap_factor=0, batch_size=5, permute=False,
                          truth_data=None, prev_truth_index=None, prev_truth_size=None):
    """
    :param truth_data:
    :param permute:
    :param overlap_factor:
    :param batch_size:
    :param model:
    :param data:
    :return:
    """
    prediction_shape = model.output_shape[-3:-1]  # patch_shape[-3:-1] #[64,64]#
    min_overlap = np.subtract(patch_shape, prediction_shape + (1,))
    max_overlap = np.subtract(patch_shape, (1, 1, 1))
    overlap = min_overlap + (overlap_factor * (max_overlap - min_overlap)).astype(np.int)
    data_0 = np.pad(data[0],
                    [(np.ceil(_ / 2).astype(int), np.floor(_ / 2).astype(int)) for _ in
                     np.subtract(patch_shape, prediction_shape + (1,))],
                    mode='constant', constant_values=np.percentile(data[0], q=1))
    data_0 = np.pad(data_0,
                    [(_, _) for _ in
                     np.ceil(np.maximum(np.subtract(patch_shape, data_0.shape) + 1, 0) / 2).astype(int)],
                    'constant', constant_values=np.percentile(data_0, q=1))

    if truth_data is not None:
        truth_0 = np.pad(truth_data[0],
                         [(np.ceil(_ / 2).astype(int), np.floor(_ / 2).astype(int)) for _ in
                          np.subtract(patch_shape, prediction_shape + (1,))],
                         mode='constant', constant_values=0)
        truth_0 = np.pad(truth_0,
                         [(_, _) for _ in
                          np.ceil(np.maximum(np.subtract(patch_shape, truth_0.shape) + 1, 0) / 2).astype(int)],
                         'constant', constant_values=0)

        truth_patch_shape = list(patch_shape[:2]) + [prev_truth_size]
    else:
        truth_0 = None
        truth_patch_shape = None

    indices = get_set_of_patch_indices_full((0, 0, 0),
                                            np.subtract(data_0.shape, patch_shape),
                                            np.subtract(patch_shape, overlap))

    # assert len(indices) * np.prod(prediction_shape[:-1]) == np.prod(data.shape), \
    #     'no total coverage for prediction, something wrong with the indexing'

    b_iter = batch_iterator(indices, batch_size, data_0, patch_shape,
                            truth_0, prev_truth_index, truth_patch_shape)
    tb_iter = iter(ThreadedGenerator(b_iter, queue_maxsize=50))

    data_shape = list(data_0.shape[-3:]) + [model.output_shape[-1]]
    predicted_output = np.zeros(data_shape)
    predicted_count = np.zeros(data_shape, dtype=np.int16)
    with tqdm(total=len(indices)) as pbar:
        for [curr_batch, batch_indices] in tb_iter:
            prediction = predict(model, np.asarray(curr_batch), permute=permute)
            prediction = np.expand_dims(prediction, -2)

            for predicted_patch, predicted_index in zip(prediction, batch_indices):
                # predictions.append(predicted_patch)
                x, y, z = predicted_index
                x_len, y_len, z_len = predicted_patch.shape[:-1]
                predicted_output[x:x + x_len, y:y + y_len, z:z + z_len, :] += predicted_patch
                predicted_count[x:x + x_len, y:y + y_len, z:z + z_len] += 1
            pbar.update(batch_size)

    # ind_offset = np.subtract(patch_shape[-3:], prediction_shape + (1,)) // 2
    # indices = [np.add(ind, ind_offset) for ind in indices]
    assert np.all(predicted_count > 0), 'Found zeros in count'
    return predicted_output / predicted_count
    # return reconstruct_from_patches(predictions, patch_indices=indices, data_shape=data_shape)


def get_prediction_labels(prediction, threshold=0.5, labels=None):
    n_samples = prediction.shape[0]
    label_arrays = []
    for sample_number in range(n_samples):
        label_data = np.argmax(prediction[sample_number], axis=1)
        label_data[np.max(prediction[sample_number], axis=0) < threshold] = 0
        if labels:
            for value in np.unique(label_data).tolist()[1:]:
                label_data[label_data == value] = labels[value - 1]
        label_arrays.append(np.array(label_data, dtype=np.uint8))
    return label_arrays


def get_test_indices(testing_file):
    return pickle_load(testing_file)


def predict_from_data_file(model, open_data_file, index):
    return model.predict(open_data_file.root.data[index])


def predict_and_get_image(model, data, affine):
    return nib.Nifti1Image(model.predict(data)[0, 0], affine)


def predict_from_data_file_and_get_image(model, open_data_file, index):
    return predict_and_get_image(model, open_data_file.root.data[index], open_data_file.root.affine)


def predict_from_data_file_and_write_image(model, open_data_file, index, out_file):
    image = predict_from_data_file_and_get_image(model, open_data_file, index)
    image.to_filename(out_file)


def prediction_to_image(prediction, label_map=False, threshold=0.5, labels=None):
    if prediction.shape[0] == 1:
        data = prediction[0]
        if label_map:
            label_map_data = np.zeros(prediction[0, 0].shape, np.int8)
            if labels:
                label = labels[0]
            else:
                label = 1
            label_map_data[data > threshold] = label
            data = label_map_data
    elif prediction.shape[1] > 1:
        if label_map:
            label_map_data = get_prediction_labels(prediction, threshold=threshold, labels=labels)
            data = label_map_data[0]
        else:
            return multi_class_prediction(prediction)
    else:
        raise RuntimeError("Invalid prediction array shape: {0}".format(prediction.shape))
    return get_image(data)


def multi_class_prediction(prediction, affine):
    prediction_images = []
    for i in range(prediction.shape[1]):
        prediction_images.append(get_image(prediction[0, i]))
    return prediction_images


def run_validation_case(data_index, output_dir, model, data_file, training_modalities, patch_shape,
                        output_label_map=False, threshold=0.5, labels=None, overlap_factor=0, permute=False,
                        prev_truth_index=None, prev_truth_size=None):
    """
    Runs a test case and writes predicted images to file.
    :param data_index: Index from of the list of test cases to get an image prediction from.
    :param output_dir: Where to write prediction images.
    :param output_label_map: If True, will write out a single image with one or more labels. Otherwise outputs
    the (sigmoid) prediction values from the model.
    :param threshold: If output_label_map is set to True, this threshold defines the value above which is 
    considered a positive result and will be assigned a label.  
    :param labels:
    :param training_modalities:
    :param data_file:
    :param model:
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    test_data = np.asarray([data_file.root.data[data_index]])
    if prev_truth_index is not None:
        test_truth_data = np.asarray([data_file.root.truth[data_index]])
    else:
        test_truth_data = None

    for i, modality in enumerate(training_modalities):
        image = get_image(test_data[i])
        image.to_filename(os.path.join(output_dir, "data_{0}.nii.gz".format(modality)))

    test_truth = get_image(data_file.root.truth[data_index])
    test_truth.to_filename(os.path.join(output_dir, "truth.nii.gz"))

    if patch_shape == test_data.shape[-3:]:
        prediction = predict(model, test_data, permute=permute)
    else:
        prediction = \
            patch_wise_prediction(model=model, data=test_data, overlap_factor=overlap_factor,
                                  patch_shape=patch_shape, permute=permute,
                                  truth_data=test_truth_data, prev_truth_index=prev_truth_index,
                                  prev_truth_size=prev_truth_size)[np.newaxis]
    if prediction.shape[-1] > 1:
        prediction = prediction[..., 1]
    prediction = prediction.squeeze()
    prediction_image = get_image(prediction)
    if isinstance(prediction_image, list):
        for i, image in enumerate(prediction_image):
            image.to_filename(os.path.join(output_dir, "prediction_{0}.nii.gz".format(i + 1)))
    else:
        prediction_image.to_filename(os.path.join(output_dir, "prediction.nii.gz"))


def run_validation_cases(validation_keys_file, model_file, training_modalities, labels, hdf5_file, patch_shape,
                         output_label_map=False, output_dir=".", threshold=0.5, overlap_factor=0,
                         permute=False, prev_truth_index=None, prev_truth_size=None):
    validation_indices = pickle_load(validation_keys_file)
    model = load_old_model(get_last_model_path(model_file))
    data_file = tables.open_file(hdf5_file, "r")
    for index in validation_indices:
        if 'subject_ids' in data_file.root:
            case_directory = os.path.join(output_dir, data_file.root.subject_ids[index].decode('utf-8'))
        else:
            case_directory = os.path.join(output_dir, "validation_case_{}".format(index))
        run_validation_case(data_index=index, output_dir=case_directory, model=model, data_file=data_file,
                            training_modalities=training_modalities, output_label_map=output_label_map, labels=labels,
                            threshold=threshold, overlap_factor=overlap_factor, permute=permute,
                            patch_shape=patch_shape,
                            prev_truth_index=prev_truth_index, prev_truth_size=prev_truth_size)
    data_file.close()


def predict(model, data, permute=False):
    if permute:
        predictions = list()
        for batch_index in range(data.shape[0]):
            predictions.append(predict_with_permutations(model, data[batch_index]))
        return np.asarray(predictions)
    else:
        return model.predict(data)


def predict_with_permutations(model, data):
    predictions = list()
    for permutation_key in generate_permutation_keys():
        temp_data = permute_data(data, permutation_key)[np.newaxis]
        predictions.append(reverse_permute_data(model.predict(temp_data)[0], permutation_key))
    return np.mean(predictions, axis=0)
