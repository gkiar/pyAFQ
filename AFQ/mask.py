import numpy as np

import nibabel as nib

import AFQ.registration as reg


def check_mask_methods(mask, mask_name=False):
    '''
    Helper function
    Checks if mask is a valid mask.
    If mask_name is not False, will throw an error stating the method
    not found and the mask name.
    '''
    error = (mask_name is not False)
    if not hasattr(mask, 'find_path'):
        if error:
            raise TypeError(f"find_path method not found in {mask_name}")
        else:
            return False
    elif not hasattr(mask, 'get_mask'):
        if error:
            raise TypeError(f"get_mask method not found in {mask_name}")
        else:
            return False
    else:
        return True


def _resample_mask(mask_data, dwi_data, mask_affine, dwi_affine):
    '''
    Helper function
    Resamples mask to dwi if necessary
    '''
    mask_type = mask_data.dtype
    if ((dwi_data is not None)
        and (dwi_affine is not None)
            and (dwi_data[..., 0].shape != mask_data.shape)):
        return np.round(reg.resample(mask_data.astype(float),
                                     dwi_data[..., 0],
                                     mask_affine,
                                     dwi_affine)).astype(mask_type)
    else:
        return mask_data


class _MaskCombiner(object):
    '''
    Helper Object
    Manages common mask combination operations
    '''

    def __init__(self, shape, combine):
        self.combine = combine
        if self.combine == "or":
            self.mask = np.zeros(shape, dtype=bool)
        elif self.combine == "and":
            self.mask = np.ones(shape, dtype=bool)
        else:
            self.combine_illdefined()

    def combine_mask(self, other_mask):
        if self.combine == "or":
            self.mask = np.logical_or(self.mask, other_mask)
        elif self.combine == "and":
            self.mask = np.logical_and(self.mask, other_mask)
        else:
            self.combine_illdefined()

    def combine_illdefined(self):
        raise TypeError((
            f"combine should be either 'or' or 'and',"
            f" you set combine to {self.combine}"))


class MaskFile(object):
    def __init__(self, suffix, scope):
        """
        Define a mask based on a file.
        Does not apply any labels or thresholds;
        Generates mask with floating point data.
        Useful for seed and stop masks, where threshold can be applied
        after interpolation (see example).

        Parameters
        ----------
        suffix : str
            Suffix to pass to bids_layout.get() to identify the file.
        scope : str, optional
            Scope to pass to bids_layout.get() to specify the pipeline
            to get the file from. If None, all scopes are searched.
            Default: None

        Examples
        --------
        seed_mask = MaskFile(
            "WM_mask",
            scope="dmriprep")
        api.AFQ(tracking_params={"seed_mask": seed_mask,
                                 "seed_threshold": 0.1})
        """
        self.suffix = suffix
        self.scope = scope
        self.fnames = {}

    def find_path(self, bids_layout, subject, session):
        if session not in self.fnames:
            self.fnames[session] = {}
        self.fnames[session][subject] = bids_layout.get(
            subject=subject, session=session,
            extension='.nii.gz',
            suffix=self.suffix,
            return_type='filename',
            scope=self.scope)[0]

    def get_path_data_affine(self, afq, row):
        mask_file = self.fnames[row['ses']][row['subject']]
        mask_img = nib.load(mask_file)
        return mask_file, mask_img.get_fdata(), mask_img.affine

    def apply_conditions(self, mask_data_orig, mask_file):
        return mask_data_orig, dict(source=mask_file)

    def get_mask(self, afq, row):
        # Load data
        dwi_data, _, dwi_img = afq._get_data_gtab(row)
        mask_file, mask_data_orig, mask_affine = \
            self.get_path_data_affine(afq, row)

        # Apply any conditions on the data
        mask_data, meta = self.apply_conditions(mask_data_orig, mask_file)

        # Resample to DWI data:
        mask_data = _resample_mask(
            mask_data,
            dwi_data,
            mask_affine,
            dwi_img.affine)

        return mask_data, meta


class FullMask(object):
    """
    Define a mask which covers a full volume.

    Examples
    --------
    brain_mask = FullMask()
    """

    def find_path(self, bids_layout, subject, session):
        pass

    def get_mask(self, afq, row):
        # Load data to get shape
        dwi_data, _, _ = afq._get_data_gtab(row)

        return np.ones(dwi_data.shape), dict(source="Entire Volume")


class LabelledMaskFile(MaskFile):
    def __init__(self, suffix, scope=None, inclusive_labels=None,
                 exclusive_labels=None, combine="or"):
        """
        Define a mask based on labels in a file.

        Parameters
        ----------
        suffix : str
            Suffix to pass to bids_layout.get() to identify the file.
        scope : str, optional
            Scope to pass to bids_layout.get() to specify the pipeline
            to get the file from. If None, all scopes are searched.
            Default: None
        inclusive_labels : list of ints, optional
            The labels from the file to include from the boolean mask.
            If None, no inclusive labels are applied.
        exclusive_labels : lits of ints, optional
            The labels from the file to exclude from the boolean mask.
            If None, no exclusive labels are applied.
            Default: None.
        combine : str, optional
            How to combine the boolean masks generated by inclusive_labels
            and exclusive_labels. If "and", they will be and'd together.
            If "or", they will be or'd.
            Note: in this class, you will most likely want to either set
            inclusive_labels or exclusive_labels, not both,
            so combine will not matter.
            Default: "or"

        Examples
        --------
        brain_mask = LabelledMaskFile(
            "aseg",
            scope="dmriprep",
            exclusive_labels=[0])
        api.AFQ(brain_mask=brain_mask)
        """
        super().__init__(suffix, scope)
        self.combine = combine
        self.ilabels = inclusive_labels
        self.elabels = exclusive_labels

    # overrides _MaskFile
    def apply_conditions(self, mask_data_orig, mask_file):
        # For different sets of labels, extract all the voxels that
        # have any / all of these values:
        mask = _MaskCombiner(mask_data_orig.shape, self.combine)
        if self.ilabels is not None:
            for label in self.ilabels:
                mask.combine_mask(mask_data_orig == label)
        if self.elabels is not None:
            for label in self.elabels:
                mask.combine_mask(mask_data_orig != label)

        meta = dict(source=mask_file,
                    inclusive_labels=self.ilabels,
                    exclusive_lavels=self.elabels,
                    combined_with=self.combine)
        return mask.mask, meta


class ThresholdedMaskFile(MaskFile):
    def __init__(self, suffix, scope=None, lower_bound=None,
                 upper_bound=None, combine="and"):
        """
        Define a mask based on thresholding a file.
        Note that this should not be used to directly make a seed mask
        or a stop mask. In those cases, consider thresholding after
        interpolation, as in the example for MaskFile.

        Parameters
        ----------
        suffix : str
            Suffix to pass to bids_layout.get() to identify the file.
        scope : str, optional
            Scope to pass to bids_layout.get() to specify the pipeline
            to get the file from. If None, all scopes are searched.
            Default: None
        lower_bound : float, optional
            Lower bound to generate boolean mask from data in the file.
            If None, no lower bound is applied.
            Default: None.
        upper_bound : float, optional
            Upper bound to generate boolean mask from data in the file.
            If None, no upper bound is applied.
            Default: None.
        combine : str, optional
            How to combine the boolean masks generated by lower_bound
            and upper_bound. If "and", they will be and'd together.
            If "or", they will be or'd.
            Default: "and"

        Examples
        --------
        brain_mask = ThresholdedMaskFile(
            "brain_mask",
            scope="dmriprep",
            lower_bound=0.1)
        api.AFQ(brain_mask=brain_mask)
        """
        super().__init__(suffix, scope)
        self.combine = combine
        self.lb = lower_bound
        self.ub = upper_bound

    # overrides _MaskFile
    def apply_conditions(self, mask_data_orig, mask_file):
        # Apply thresholds
        mask = _MaskCombiner(mask_data_orig.shape, self.combine)
        if self.ub is not None:
            mask.combine_mask(mask_data_orig < self.ub)
        if self.lb is not None:
            mask.combine_mask(mask_data_orig > self.lb)

        meta = dict(source=mask_file,
                    upper_bound=self.ub,
                    lower_bound=self.lb,
                    combined_with=self.combine)
        return mask.mask, meta


class ScalarMask(MaskFile):
    def __init__(self, scalar):
        """
        Define a mask based on a scalar.
        Does not apply any labels or thresholds;
        Generates mask with floating point data.
        Useful for seed and stop masks, where threshold can be applied
        after interpolation (see example).

        Parameters
        ----------
        scalar : str
            Scalar to threshold.
            Can be one of "dti_fa", "dti_md", "dki_fa", "dki_md".

        Examples
        --------
        seed_mask = ScalarMask(
            "dti_fa",
            scope="dmriprep")
        api.AFQ(tracking_params={"seed_mask": seed_mask,
                                 "seed_threshold": 0.2})
        """
        self.scalar_name = scalar

    # overrides _MaskFile
    def find_path(self, bids_layout, subject, session):
        pass

    # overrides _MaskFile
    def get_path_data_affine(self, afq, row):
        valid_scalars = list(afq._scalar_dict.keys())
        if self.scalar_name not in valid_scalars:
            raise RuntimeError((
                f"scalar should be one of"
                f" {', '.join(valid_scalars)}"
                f", you input {self.scalar_name}"))

        scalar_fname = afq._scalar_dict[self.scalar_name](afq, row)
        scalar_img = nib.load(scalar_fname)
        scalar_data = scalar_img.get_fdata()

        return scalar_fname, scalar_data, scalar_img.affine


class ThresholdedScalarMask(ThresholdedMaskFile, ScalarMask):
    def __init__(self, scalar, lower_bound=None, upper_bound=None,
                 combine="and"):
        """
        Define a mask based on thresholding a scalar mask.
        Note that this should not be used to directly make a seed mask
        or a stop mask. In those cases, consider thresholding after
        interpolation, as in the example for ScalarMask.

        Parameters
        ----------
        scalar : str
            Scalar to threshold.
            Can be one of "dti_fa", "dti_md", "dki_fa", "dki_md".
        lower_bound : float, optional
            Lower bound to generate boolean mask from data in the file.
            If None, no lower bound is applied.
            Default: None.
        upper_bound : float, optional
            Upper bound to generate boolean mask from data in the file.
            If None, no upper bound is applied.
            Default: None.
        combine : str, optional
            How to combine the boolean masks generated by lower_bound
            and upper_bound. If "and", they will be and'd together.
            If "or", they will be or'd.
            Default: "and"

        Examples
        --------
        seed_mask = ThresholdedScalarMask(
            "dti_fa",
            lower_bound=0.2)
        api.AFQ(tracking_params={"seed_mask": seed_mask})
        """
        self.scalar_name = scalar
        self.combine = combine
        self.lb = lower_bound
        self.ub = upper_bound


class CombinedMask(object):
    def __init__(self, mask_list, combine="and"):
        """
        Define a mask by combining other masks.

        Parameters
        ----------
        mask_list : list of Masks with find_path and get_mask functions
            List of masks to combine. All find_path methods will be called
            when this find_path method is called. All get_mask methods will
            be called and combined when this get_mask method is called.
        combine : str, optional
            How to combine the boolean masks generated by mask_list.
            If "and", they will be and'd together.
            If "or", they will be or'd.
            Default: "and"

        Examples
        --------
        seed_mask = CombinedMask(
            [ThresholdedScalarMask(
                "dti_fa",
                lower_bound=0.2),
            ThresholdedScalarMask(
                "dti_md",
                upper_bound=0.002)])
        api.AFQ(tracking_params={"seed_mask": seed_mask})
        """
        self.mask_list = mask_list
        self.combine = combine

    def find_path(self, bids_layout, subject, session):
        for mask in self.mask_list:
            mask.find_path(bids_layout, subject, session)

    def get_mask(self, afq, row):
        mask = None
        metas = []
        for mask in self.mask_list:
            next_mask, next_meta = mask.get_mask(afq, row)
            if mask is None:
                mask = _MaskCombiner(next_mask.shape, self.combine)
            else:
                mask.combine_mask(next_mask)
            metas.append(next_meta)

        meta = dict(sources=metas,
                    combined_with=self.combine)

        return mask.mask, meta
