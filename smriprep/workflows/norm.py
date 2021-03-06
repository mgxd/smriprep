# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
Spatial normalization workflows.

.. autofunction:: init_anat_norm_wf

"""
from collections import defaultdict
from nipype.pipeline import engine as pe
from nipype.interfaces import utility as niu

from nipype.interfaces.ants.base import Info as ANTsInfo

from templateflow.api import get_metadata, templates as get_templates
from niworkflows.engine.workflows import LiterateWorkflow as Workflow
from niworkflows.interfaces.ants import ImageMath
from niworkflows.interfaces.mni import RobustMNINormalization
from niworkflows.interfaces.fixes import FixHeaderApplyTransforms as ApplyTransforms
from ..interfaces.templateflow import TemplateFlowSelect


def init_anat_norm_wf(
    debug,
    omp_nthreads,
    templates,
):
    """
    Build an individual spatial normalization workflow using ``antsRegistration``.

    .. workflow ::
        :graph2use: orig
        :simple_form: yes

        from smriprep.workflows.norm import init_anat_norm_wf
        wf = init_anat_norm_wf(
            debug=False,
            omp_nthreads=1,
            template_list=['MNI152NLin2009cAsym', 'MNI152NLin6Asym'],
        )

    **Parameters**

        debug : bool
            Apply sloppy arguments to speed up processing. Use with caution,
            registration processes will be very inaccurate.
        omp_nthreads : int
            Maximum number of threads an individual process may use.
        templates : list of tuples
            List of tuples containing TemplateFlow identifiers (e.g. ``MNI152NLin6Asym``)
            and corresponding specs, which specify target templates
            for spatial normalization.

    **Inputs**

        moving_image
            The input image that will be normalized to standard space.
        moving_mask
            A precise brain mask separating skull/skin/fat from brain
            structures.
        moving_segmentation
            A brain tissue segmentation of the ``moving_image``.
        moving_tpms
            tissue probability maps (TPMs) corresponding to the
            ``moving_segmentation``.
        lesion_mask
            (optional) A mask to exclude regions from the cost-function
            input domain to enable standardization of lesioned brains.
        orig_t1w
            The original T1w image from the BIDS structure.

    **Outputs**

        standardized
            The T1w after spatial normalization, in template space.
        anat2std_xfm
            The T1w-to-template transform.
        std2anat_xfm
            The template-to-T1w transform.
        std_mask
            The ``moving_mask`` in template space (matches ``standardized`` output).
        std_dseg
            The ``moving_segmentation`` in template space (matches ``standardized``
            output).
        std_tpms
            The ``moving_tpms`` in template space (matches ``standardized`` output).
        template
            The input parameter ``template`` for further use in nodes depending
            on this
            workflow.

    """
    templateflow = get_templates()
    missing_tpls = [template for template, _ in templates if template not in templateflow]
    if missing_tpls:
        raise ValueError("""\
One or more templates were not found (%s). Please make sure TemplateFlow is \
correctly installed and contains the given template identifiers.""" % ', '.join(missing_tpls))

    ntpls = len(templates)
    workflow = Workflow('anat_norm_wf')
    workflow.__desc__ = """\
Volume-based spatial normalization to {targets} ({targets_id}) was performed through
nonlinear registration with `antsRegistration` (ANTs {ants_ver}),
using brain-extracted versions of both T1w reference and the T1w template.
The following template{tpls} selected for spatial normalization:
""".format(
        ants_ver=ANTsInfo.version() or '(version unknown)',
        targets='%s standard space%s' % (defaultdict(
            'several'.format, {1: 'one', 2: 'two', 3: 'three', 4: 'four'})[ntpls],
            's' * (ntpls != 1)),
        targets_id=', '.join((t for t, _ in templates)),
        tpls=(' was', 's were')[ntpls != 1]
    )

    # Append template citations to description
    for template, _ in templates:
        template_meta = get_metadata(template)
        template_refs = ['@%s' % template.lower()]

        if template_meta.get('RRID', None):
            template_refs += ['RRID:%s' % template_meta['RRID']]

        workflow.__desc__ += """\
*{template_name}* [{template_refs}; TemplateFlow ID: {template}]""".format(
            template=template,
            template_name=template_meta['Name'],
            template_refs=', '.join(template_refs))
        workflow.__desc__ += (', ', '.')[template == templates[-1][0]]

    inputnode = pe.Node(niu.IdentityInterface(fields=[
        'moving_image', 'moving_mask', 'moving_segmentation', 'moving_tpms',
        'lesion_mask', 'orig_t1w', 'template']),
        name='inputnode')
    inputnode.iterables = [('template', templates)]
    out_fields = ['standardized', 'anat2std_xfm', 'std2anat_xfm',
                  'std_mask', 'std_dseg', 'std_tpms', 'template']
    poutputnode = pe.Node(niu.IdentityInterface(fields=out_fields), name='poutputnode')

    tf_select = pe.Node(TemplateFlowSelect(resolution=1 + debug),
                        name='tf_select', run_without_submitting=True)

    # With the improvements from poldracklab/niworkflows#342 this truncation is now necessary
    trunc_mov = pe.Node(ImageMath(operation='TruncateImageIntensity', op2='0.01 0.999 256'),
                        name='trunc_mov')

    registration = pe.Node(RobustMNINormalization(
        float=True, flavor=['precise', 'testing'][debug],
    ), name='registration', n_procs=omp_nthreads, mem_gb=2)

    # Resample T1w-space inputs
    tpl_moving = pe.Node(ApplyTransforms(
        dimension=3, default_value=0, float=True,
        interpolation='LanczosWindowedSinc'), name='tpl_moving')
    std_mask = pe.Node(ApplyTransforms(dimension=3, default_value=0, float=True,
                                       interpolation='MultiLabel'), name='std_mask')

    std_dseg = pe.Node(ApplyTransforms(dimension=3, default_value=0, float=True,
                                       interpolation='MultiLabel'), name='std_dseg')

    std_tpms = pe.MapNode(ApplyTransforms(dimension=3, default_value=0, float=True,
                                          interpolation='Gaussian'),
                          iterfield=['input_image'], name='std_tpms')

    workflow.connect([
        (inputnode, tf_select, [(('template', _get_name), 'template'),
                                (('template', _get_spec), 'template_spec')]),
        (inputnode, registration, [(('template', _get_name), 'template'),
                                   (('template', _get_spec), 'template_spec')]),
        (inputnode, trunc_mov, [('moving_image', 'op1')]),
        (inputnode, registration, [
            ('moving_mask', 'moving_mask'),
            ('lesion_mask', 'lesion_mask')]),
        (inputnode, tpl_moving, [('moving_image', 'input_image')]),
        (inputnode, std_mask, [('moving_mask', 'input_image')]),
        (tf_select, tpl_moving, [('t1w_file', 'reference_image')]),
        (tf_select, std_mask, [('t1w_file', 'reference_image')]),
        (tf_select, std_dseg, [('t1w_file', 'reference_image')]),
        (tf_select, std_tpms, [('t1w_file', 'reference_image')]),
        (trunc_mov, registration, [
            ('output_image', 'moving_image')]),
        (registration, tpl_moving, [('composite_transform', 'transforms')]),
        (registration, std_mask, [('composite_transform', 'transforms')]),
        (inputnode, std_dseg, [('moving_segmentation', 'input_image')]),
        (registration, std_dseg, [('composite_transform', 'transforms')]),
        (inputnode, std_tpms, [('moving_tpms', 'input_image')]),
        (registration, std_tpms, [('composite_transform', 'transforms')]),
        (registration, poutputnode, [
            ('composite_transform', 'anat2std_xfm'),
            ('inverse_composite_transform', 'std2anat_xfm')]),
        (tpl_moving, poutputnode, [('output_image', 'standardized')]),
        (std_mask, poutputnode, [('output_image', 'std_mask')]),
        (std_dseg, poutputnode, [('output_image', 'std_dseg')]),
        (std_tpms, poutputnode, [('output_image', 'std_tpms')]),
        (inputnode, poutputnode, [('template', 'template')]),
    ])

    # Provide synchronized output
    outputnode = pe.JoinNode(niu.IdentityInterface(fields=out_fields),
                             name='outputnode', joinsource='inputnode')
    workflow.connect([
        (poutputnode, outputnode, [(f, f) for f in out_fields]),
    ])

    return workflow


def _get_name(in_tuple):
    return in_tuple[0]


def _get_spec(in_tuple):
    return in_tuple[1]
