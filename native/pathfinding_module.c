#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <atrinik/pathfinding.h>

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

typedef struct grid_adapter {
    size_t width;
    size_t height;
    const uint8_t *walkable;
    const uint8_t *costs;
    const uint8_t *excluded;
    const uint8_t *goals;
    const size_t *goal_states;
    size_t goal_count;
    bool diagonal;
} grid_adapter;

typedef struct graph_adapter {
    const uint64_t *offsets;
    const uint64_t *targets;
    const uint64_t *costs;
    const uint64_t *metadata;
    const uint8_t *goals;
    const uint8_t *blocked_states;
    const uint8_t *excluded_edges;
} graph_adapter;

static int dict_set_owned(PyObject *dictionary, const char *key, PyObject *value) {
    if (value == NULL) {
        return -1;
    }
    int result = PyDict_SetItemString(dictionary, key, value);
    Py_DECREF(value);
    return result;
}

static PyObject *metrics_dict(const atrinik_pf_metrics *metrics) {
    PyObject *dictionary = PyDict_New();
    if (dictionary == NULL ||
        dict_set_owned(dictionary, "expanded", PyLong_FromSize_t(metrics->expanded)) < 0 ||
        dict_set_owned(dictionary, "generated", PyLong_FromSize_t(metrics->generated)) < 0 ||
        dict_set_owned(dictionary,
                       "examined_transitions",
                       PyLong_FromSize_t(metrics->examined_transitions)) < 0 ||
        dict_set_owned(dictionary, "peak_frontier", PyLong_FromSize_t(metrics->peak_frontier)) <
            0 ||
        dict_set_owned(dictionary, "total_cost", PyLong_FromUnsignedLongLong(metrics->total_cost)) <
            0) {
        Py_XDECREF(dictionary);
        return NULL;
    }
    return dictionary;
}

static PyObject *search_result_dict(const atrinik_pf_result *result) {
    if (result->step_count > (size_t)PY_SSIZE_T_MAX) {
        PyErr_SetString(PyExc_OverflowError, "path is too large for a Python list");
        return NULL;
    }
    PyObject *dictionary = PyDict_New();
    PyObject *path = PyList_New((Py_ssize_t)result->step_count);
    PyObject *transitions =
        PyList_New(result->step_count == 0U ? 0 : (Py_ssize_t)(result->step_count - 1U));
    if (dictionary == NULL || path == NULL || transitions == NULL) {
        Py_XDECREF(dictionary);
        Py_XDECREF(path);
        Py_XDECREF(transitions);
        return NULL;
    }

    for (size_t i = 0U; i < result->step_count; i++) {
        PyObject *state = PyLong_FromUnsignedLongLong(result->steps[i].state);
        if (state == NULL) {
            Py_DECREF(dictionary);
            Py_DECREF(path);
            Py_DECREF(transitions);
            return NULL;
        }
        PyList_SET_ITEM(path, (Py_ssize_t)i, state);
        if (i != 0U) {
            PyObject *metadata = PyLong_FromUnsignedLongLong(result->steps[i].data);
            if (metadata == NULL) {
                Py_DECREF(dictionary);
                Py_DECREF(path);
                Py_DECREF(transitions);
                return NULL;
            }
            PyList_SET_ITEM(transitions, (Py_ssize_t)(i - 1U), metadata);
        }
    }

    if (dict_set_owned(dictionary,
                       "status",
                       PyUnicode_FromString(atrinik_pf_status_string(result->status))) < 0 ||
        PyDict_SetItemString(dictionary, "path", path) < 0 ||
        PyDict_SetItemString(dictionary, "transitions", transitions) < 0 ||
        dict_set_owned(dictionary, "metrics", metrics_dict(&result->metrics)) < 0) {
        Py_DECREF(dictionary);
        Py_DECREF(path);
        Py_DECREF(transitions);
        return NULL;
    }
    Py_DECREF(path);
    Py_DECREF(transitions);
    return dictionary;
}

static PyObject *reachability_result_dict(const atrinik_pf_reachability_result *result) {
    if (result->state_count > (size_t)PY_SSIZE_T_MAX) {
        PyErr_SetString(PyExc_OverflowError, "state set is too large for a Python list");
        return NULL;
    }
    PyObject *dictionary = PyDict_New();
    PyObject *states = PyList_New((Py_ssize_t)result->state_count);
    if (dictionary == NULL || states == NULL) {
        Py_XDECREF(dictionary);
        Py_XDECREF(states);
        return NULL;
    }
    for (size_t i = 0U; i < result->state_count; i++) {
        PyObject *state = PyLong_FromUnsignedLongLong(result->states[i]);
        if (state == NULL) {
            Py_DECREF(dictionary);
            Py_DECREF(states);
            return NULL;
        }
        PyList_SET_ITEM(states, (Py_ssize_t)i, state);
    }
    if (dict_set_owned(dictionary,
                       "status",
                       PyUnicode_FromString(atrinik_pf_status_string(result->status))) < 0 ||
        PyDict_SetItemString(dictionary, "states", states) < 0 ||
        dict_set_owned(dictionary, "metrics", metrics_dict(&result->metrics)) < 0) {
        Py_DECREF(dictionary);
        Py_DECREF(states);
        return NULL;
    }
    Py_DECREF(states);
    return dictionary;
}

static int byte_buffer(PyObject *object,
                       const char *name,
                       size_t expected,
                       bool optional,
                       Py_buffer *view,
                       const uint8_t **data) {
    *data = NULL;
    view->obj = NULL;
    if (object == Py_None && optional) {
        return 0;
    }
    if (PyObject_GetBuffer(object, view, PyBUF_SIMPLE) < 0) {
        return -1;
    }
    if (!view->readonly) {
        PyErr_Format(PyExc_TypeError, "%s must be an immutable byte buffer", name);
        PyBuffer_Release(view);
        view->obj = NULL;
        return -1;
    }
    if ((size_t)view->len != expected) {
        PyErr_Format(PyExc_ValueError,
                     "%s must contain exactly %zu bytes, got %zd",
                     name,
                     expected,
                     view->len);
        PyBuffer_Release(view);
        view->obj = NULL;
        return -1;
    }
    *data = view->buf;
    return 0;
}

static bool grid_neighbors(void *context,
                           atrinik_pf_state_id state,
                           atrinik_pf_emit_fn emit,
                           void *emit_context) {
    static const int8_t offsets[][2] = {
        {0, -1},
        {1, 0},
        {0, 1},
        {-1, 0},
        {1, -1},
        {1, 1},
        {-1, 1},
        {-1, -1},
    };
    grid_adapter *grid = context;
    size_t x = (size_t)state % grid->width;
    size_t y = (size_t)state / grid->width;
    size_t count = grid->diagonal ? sizeof(offsets) / sizeof(offsets[0]) : 4U;
    for (size_t i = 0U; i < count; i++) {
        int64_t next_x = (int64_t)x + offsets[i][0];
        int64_t next_y = (int64_t)y + offsets[i][1];
        if (next_x < 0 || next_y < 0 || (uint64_t)next_x >= grid->width ||
            (uint64_t)next_y >= grid->height) {
            continue;
        }
        size_t next = (size_t)next_y * grid->width + (size_t)next_x;
        if (grid->walkable[next] == 0U || (grid->excluded != NULL && grid->excluded[next] != 0U)) {
            continue;
        }
        atrinik_pf_transition transition = {
            .state = next,
            .cost = grid->costs == NULL ? 1U : grid->costs[next],
            .data = i,
        };
        if (!emit(emit_context, &transition)) {
            return false;
        }
    }
    return true;
}

static bool grid_goal(void *context, atrinik_pf_state_id state) {
    grid_adapter *grid = context;
    return grid->goals[state] != 0U;
}

static uint64_t grid_partial_rank(void *context, atrinik_pf_state_id state) {
    grid_adapter *grid = context;
    size_t state_x = (size_t)state % grid->width;
    size_t state_y = (size_t)state / grid->width;
    uint64_t best = UINT64_MAX;
    for (size_t i = 0U; i < grid->goal_count; i++) {
        size_t goal = grid->goal_states[i];
        size_t goal_x = goal % grid->width;
        size_t goal_y = goal / grid->width;
        size_t dx = state_x > goal_x ? state_x - goal_x : goal_x - state_x;
        size_t dy = state_y > goal_y ? state_y - goal_y : goal_y - state_y;
        uint64_t distance = grid->diagonal ? (uint64_t)(dx > dy ? dx : dy) : (uint64_t)(dx + dy);
        if (distance < best) {
            best = distance;
        }
    }
    return best;
}

static PyObject *py_grid_search(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    Py_ssize_t width_value;
    Py_ssize_t height_value;
    PyObject *walkable_object;
    unsigned long long start;
    PyObject *goals_object;
    PyObject *costs_object = Py_None;
    int diagonal = 1;
    Py_ssize_t max_generated = 0;
    int return_partial = 0;
    PyObject *excluded_object = Py_None;
    static char *keywords[] = {"width",
                               "height",
                               "walkable",
                               "start",
                               "goals",
                               "costs",
                               "diagonal",
                               "max_generated",
                               "return_partial",
                               "excluded",
                               NULL};
    if (!PyArg_ParseTupleAndKeywords(args,
                                     kwargs,
                                     "nnOKO|OpnpO:grid_search",
                                     keywords,
                                     &width_value,
                                     &height_value,
                                     &walkable_object,
                                     &start,
                                     &goals_object,
                                     &costs_object,
                                     &diagonal,
                                     &max_generated,
                                     &return_partial,
                                     &excluded_object)) {
        return NULL;
    }
    if (width_value <= 0 || height_value <= 0 ||
        (size_t)width_value > SIZE_MAX / (size_t)height_value || max_generated < 0) {
        PyErr_SetString(PyExc_ValueError, "grid dimensions and budgets must be valid");
        return NULL;
    }
    size_t width = (size_t)width_value;
    size_t height = (size_t)height_value;
    size_t state_count = width * height;
    if (state_count > (size_t)PY_SSIZE_T_MAX || start >= state_count) {
        PyErr_SetString(PyExc_ValueError, "start state is outside the grid");
        return NULL;
    }

    Py_buffer walkable_view = {0};
    Py_buffer goals_view = {0};
    Py_buffer costs_view = {0};
    Py_buffer excluded_view = {0};
    const uint8_t *walkable;
    const uint8_t *goals;
    const uint8_t *costs;
    const uint8_t *excluded;
    if (byte_buffer(walkable_object, "walkable", state_count, false, &walkable_view, &walkable) <
            0 ||
        byte_buffer(goals_object, "goals", state_count, false, &goals_view, &goals) < 0 ||
        byte_buffer(costs_object, "costs", state_count, true, &costs_view, &costs) < 0 ||
        byte_buffer(excluded_object, "excluded", state_count, true, &excluded_view, &excluded) <
            0) {
        if (walkable_view.obj != NULL) {
            PyBuffer_Release(&walkable_view);
        }
        if (goals_view.obj != NULL) {
            PyBuffer_Release(&goals_view);
        }
        if (costs_view.obj != NULL) {
            PyBuffer_Release(&costs_view);
        }
        return NULL;
    }

    size_t goal_count = 0U;
    for (size_t state = 0U; state < state_count; state++) {
        goal_count += goals[state] != 0U;
    }
    if (goal_count > SIZE_MAX / sizeof(size_t)) {
        PyErr_NoMemory();
        PyBuffer_Release(&walkable_view);
        PyBuffer_Release(&goals_view);
        if (costs_view.obj != NULL) {
            PyBuffer_Release(&costs_view);
        }
        if (excluded_view.obj != NULL) {
            PyBuffer_Release(&excluded_view);
        }
        return NULL;
    }
    size_t *goal_states = goal_count == 0U ? NULL : malloc(goal_count * sizeof(*goal_states));
    if (goal_count != 0U && goal_states == NULL) {
        PyErr_NoMemory();
        PyBuffer_Release(&walkable_view);
        PyBuffer_Release(&goals_view);
        if (costs_view.obj != NULL) {
            PyBuffer_Release(&costs_view);
        }
        if (excluded_view.obj != NULL) {
            PyBuffer_Release(&excluded_view);
        }
        return NULL;
    }
    size_t goal_index = 0U;
    for (size_t state = 0U; state < state_count; state++) {
        if (goals[state] != 0U) {
            goal_states[goal_index++] = state;
        }
    }

    grid_adapter grid = {
        .width = width,
        .height = height,
        .walkable = walkable,
        .costs = costs,
        .excluded = excluded,
        .goals = goals,
        .goal_states = goal_states,
        .goal_count = goal_count,
        .diagonal = diagonal != 0,
    };
    atrinik_pf_adapter adapter = {
        .context = &grid,
        .neighbors = grid_neighbors,
        .goal = grid_goal,
        .partial_rank = grid_partial_rank,
    };
    atrinik_pf_options options;
    atrinik_pf_options_init(&options);
    options.algorithm = costs == NULL ? ATRINIK_PF_BREADTH_FIRST : ATRINIK_PF_DIJKSTRA;
    options.max_generated = (size_t)max_generated;
    options.return_partial = return_partial != 0;
    atrinik_pf_context *context = atrinik_pf_context_create();
    PyObject *output = NULL;
    if (context == NULL) {
        PyErr_NoMemory();
    } else {
        atrinik_pf_result result;
        Py_BEGIN_ALLOW_THREADS result = atrinik_pf_search(context, &adapter, start, &options);
        Py_END_ALLOW_THREADS output = search_result_dict(&result);
    }
    atrinik_pf_context_destroy(context);
    free(goal_states);
    PyBuffer_Release(&walkable_view);
    PyBuffer_Release(&goals_view);
    if (costs_view.obj != NULL) {
        PyBuffer_Release(&costs_view);
    }
    if (excluded_view.obj != NULL) {
        PyBuffer_Release(&excluded_view);
    }
    return output;
}

static PyObject *py_grid_reachable(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    Py_ssize_t width_value;
    Py_ssize_t height_value;
    PyObject *walkable_object;
    unsigned long long start;
    PyObject *excluded_object = Py_None;
    int diagonal = 1;
    Py_ssize_t max_generated = 0;
    static char *keywords[] =
        {"width", "height", "walkable", "start", "excluded", "diagonal", "max_generated", NULL};
    if (!PyArg_ParseTupleAndKeywords(args,
                                     kwargs,
                                     "nnOK|Opn:grid_reachable",
                                     keywords,
                                     &width_value,
                                     &height_value,
                                     &walkable_object,
                                     &start,
                                     &excluded_object,
                                     &diagonal,
                                     &max_generated)) {
        return NULL;
    }
    if (width_value <= 0 || height_value <= 0 ||
        (size_t)width_value > SIZE_MAX / (size_t)height_value || max_generated < 0) {
        PyErr_SetString(PyExc_ValueError, "grid dimensions and budgets must be valid");
        return NULL;
    }
    size_t width = (size_t)width_value;
    size_t height = (size_t)height_value;
    size_t state_count = width * height;
    if (state_count > (size_t)PY_SSIZE_T_MAX || start >= state_count) {
        PyErr_SetString(PyExc_ValueError, "start state is outside the grid");
        return NULL;
    }

    Py_buffer walkable_view = {0};
    Py_buffer excluded_view = {0};
    const uint8_t *walkable;
    const uint8_t *excluded;
    if (byte_buffer(walkable_object, "walkable", state_count, false, &walkable_view, &walkable) <
            0 ||
        byte_buffer(excluded_object, "excluded", state_count, true, &excluded_view, &excluded) <
            0) {
        if (walkable_view.obj != NULL) {
            PyBuffer_Release(&walkable_view);
        }
        return NULL;
    }

    grid_adapter grid = {
        .width = width,
        .height = height,
        .walkable = walkable,
        .excluded = excluded,
        .diagonal = diagonal != 0,
    };
    atrinik_pf_adapter adapter = {
        .context = &grid,
        .neighbors = grid_neighbors,
    };
    atrinik_pf_options options;
    atrinik_pf_options_init(&options);
    options.max_generated = (size_t)max_generated;
    atrinik_pf_context *context = atrinik_pf_context_create();
    PyObject *output = NULL;
    if (context == NULL) {
        PyErr_NoMemory();
    } else {
        atrinik_pf_reachability_result result;
        Py_BEGIN_ALLOW_THREADS result = atrinik_pf_reachable(context, &adapter, start, &options);
        Py_END_ALLOW_THREADS output = reachability_result_dict(&result);
    }
    atrinik_pf_context_destroy(context);
    PyBuffer_Release(&walkable_view);
    if (excluded_view.obj != NULL) {
        PyBuffer_Release(&excluded_view);
    }
    return output;
}

static int sequence_to_u64(PyObject *object, const char *name, uint64_t **values, size_t *count) {
    PyObject *sequence = PySequence_Fast(object, name);
    if (sequence == NULL) {
        return -1;
    }
    Py_ssize_t length = PySequence_Fast_GET_SIZE(sequence);
    if (length < 0 || (size_t)length > SIZE_MAX / sizeof(**values)) {
        Py_DECREF(sequence);
        PyErr_NoMemory();
        return -1;
    }
    uint64_t *output = length == 0 ? NULL : malloc((size_t)length * sizeof(*output));
    if (length != 0 && output == NULL) {
        Py_DECREF(sequence);
        PyErr_NoMemory();
        return -1;
    }
    PyObject **items = PySequence_Fast_ITEMS(sequence);
    for (Py_ssize_t i = 0; i < length; i++) {
        output[i] = PyLong_AsUnsignedLongLong(items[i]);
        if (PyErr_Occurred()) {
            free(output);
            Py_DECREF(sequence);
            PyErr_Clear();
            PyErr_Format(PyExc_ValueError, "%s contains an invalid integer at index %zd", name, i);
            return -1;
        }
    }
    Py_DECREF(sequence);
    *values = output;
    *count = (size_t)length;
    return 0;
}

static bool graph_neighbors(void *context,
                            atrinik_pf_state_id state,
                            atrinik_pf_emit_fn emit,
                            void *emit_context) {
    graph_adapter *graph = context;
    size_t begin = (size_t)graph->offsets[state];
    size_t end = (size_t)graph->offsets[state + 1U];
    for (size_t edge = begin; edge < end; edge++) {
        size_t target = (size_t)graph->targets[edge];
        if ((graph->blocked_states != NULL && graph->blocked_states[target] != 0U) ||
            (graph->excluded_edges != NULL && graph->excluded_edges[edge] != 0U)) {
            continue;
        }
        atrinik_pf_transition transition = {
            .state = target,
            .cost = graph->costs == NULL ? 1U : graph->costs[edge],
            .data = graph->metadata == NULL ? edge : graph->metadata[edge],
        };
        if (!emit(emit_context, &transition)) {
            return false;
        }
    }
    return true;
}

static bool graph_goal(void *context, atrinik_pf_state_id state) {
    graph_adapter *graph = context;
    return graph->goals[state] != 0U;
}

static uint64_t graph_partial_rank(void *context, atrinik_pf_state_id state) {
    graph_adapter *graph = context;
    return graph->goals[state] != 0U ? 0U : 1U;
}

static PyObject *py_graph_search(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    PyObject *offsets_object;
    PyObject *targets_object;
    unsigned long long start;
    PyObject *goals_object;
    PyObject *costs_object = Py_None;
    PyObject *metadata_object = Py_None;
    PyObject *blocked_object = Py_None;
    PyObject *excluded_object = Py_None;
    Py_ssize_t max_generated = 0;
    int return_partial = 0;
    static char *keywords[] = {"offsets",
                               "targets",
                               "start",
                               "goals",
                               "costs",
                               "metadata",
                               "blocked_states",
                               "excluded_edges",
                               "max_generated",
                               "return_partial",
                               NULL};
    if (!PyArg_ParseTupleAndKeywords(args,
                                     kwargs,
                                     "OOKO|OOOOnp:graph_search",
                                     keywords,
                                     &offsets_object,
                                     &targets_object,
                                     &start,
                                     &goals_object,
                                     &costs_object,
                                     &metadata_object,
                                     &blocked_object,
                                     &excluded_object,
                                     &max_generated,
                                     &return_partial)) {
        return NULL;
    }
    if (max_generated < 0) {
        PyErr_SetString(PyExc_ValueError, "max_generated must be non-negative");
        return NULL;
    }

    uint64_t *offsets = NULL;
    uint64_t *targets = NULL;
    uint64_t *costs = NULL;
    uint64_t *metadata = NULL;
    size_t offsets_count = 0U;
    size_t targets_count = 0U;
    size_t costs_count = 0U;
    size_t metadata_count = 0U;
    if (sequence_to_u64(offsets_object, "offsets", &offsets, &offsets_count) < 0 ||
        sequence_to_u64(targets_object, "targets", &targets, &targets_count) < 0 ||
        (costs_object != Py_None &&
         sequence_to_u64(costs_object, "costs", &costs, &costs_count) < 0) ||
        (metadata_object != Py_None &&
         sequence_to_u64(metadata_object, "metadata", &metadata, &metadata_count) < 0)) {
        free(offsets);
        free(targets);
        free(costs);
        free(metadata);
        return NULL;
    }
    if (offsets_count < 2U) {
        PyErr_SetString(PyExc_ValueError, "offsets must describe at least one state");
        goto invalid_graph;
    }
    size_t state_count = offsets_count - 1U;
    if (start >= state_count || offsets[0] != 0U || offsets[state_count] != targets_count ||
        (costs != NULL && costs_count != targets_count) ||
        (metadata != NULL && metadata_count != targets_count)) {
        PyErr_SetString(PyExc_ValueError, "graph arrays have inconsistent sizes or start state");
        goto invalid_graph;
    }
    for (size_t state = 0U; state < state_count; state++) {
        if (offsets[state] > offsets[state + 1U] || offsets[state + 1U] > targets_count) {
            PyErr_SetString(PyExc_ValueError, "graph offsets must be monotonic and in range");
            goto invalid_graph;
        }
    }
    for (size_t edge = 0U; edge < targets_count; edge++) {
        if (targets[edge] >= state_count) {
            PyErr_SetString(PyExc_ValueError, "graph target is outside the state range");
            goto invalid_graph;
        }
    }

    Py_buffer goals_view = {0};
    Py_buffer blocked_view = {0};
    Py_buffer excluded_view = {0};
    const uint8_t *goals;
    const uint8_t *blocked;
    const uint8_t *excluded;
    if (byte_buffer(goals_object, "goals", state_count, false, &goals_view, &goals) < 0 ||
        byte_buffer(blocked_object, "blocked_states", state_count, true, &blocked_view, &blocked) <
            0 ||
        byte_buffer(excluded_object,
                    "excluded_edges",
                    targets_count,
                    true,
                    &excluded_view,
                    &excluded) < 0) {
        if (goals_view.obj != NULL) {
            PyBuffer_Release(&goals_view);
        }
        if (blocked_view.obj != NULL) {
            PyBuffer_Release(&blocked_view);
        }
        goto invalid_graph;
    }

    graph_adapter graph = {
        .offsets = offsets,
        .targets = targets,
        .costs = costs,
        .metadata = metadata,
        .goals = goals,
        .blocked_states = blocked,
        .excluded_edges = excluded,
    };
    atrinik_pf_adapter adapter = {
        .context = &graph,
        .neighbors = graph_neighbors,
        .goal = graph_goal,
        .partial_rank = graph_partial_rank,
    };
    atrinik_pf_options options;
    atrinik_pf_options_init(&options);
    options.algorithm = costs == NULL ? ATRINIK_PF_BREADTH_FIRST : ATRINIK_PF_DIJKSTRA;
    options.max_generated = (size_t)max_generated;
    options.return_partial = return_partial != 0;
    atrinik_pf_context *context = atrinik_pf_context_create();
    PyObject *output = NULL;
    if (context == NULL) {
        PyErr_NoMemory();
    } else {
        atrinik_pf_result result;
        Py_BEGIN_ALLOW_THREADS result = atrinik_pf_search(context, &adapter, start, &options);
        Py_END_ALLOW_THREADS output = search_result_dict(&result);
    }
    atrinik_pf_context_destroy(context);
    PyBuffer_Release(&goals_view);
    if (blocked_view.obj != NULL) {
        PyBuffer_Release(&blocked_view);
    }
    if (excluded_view.obj != NULL) {
        PyBuffer_Release(&excluded_view);
    }
    free(offsets);
    free(targets);
    free(costs);
    free(metadata);
    return output;

invalid_graph:
    free(offsets);
    free(targets);
    free(costs);
    free(metadata);
    return NULL;
}

PyDoc_STRVAR(module_doc, "Buffer-oriented Python binding for libatrinik's pathfinding core.");

static PyMethodDef methods[] = {
    {"grid_search",
     (PyCFunction)(void (*)(void))py_grid_search,
     METH_VARARGS | METH_KEYWORDS,
     "Search a compact walkability grid without Python callbacks."},
    {"grid_reachable",
     (PyCFunction)(void (*)(void))py_grid_reachable,
     METH_VARARGS | METH_KEYWORDS,
     "Return the start state's reachable grid component."},
    {"graph_search",
     (PyCFunction)(void (*)(void))py_graph_search,
     METH_VARARGS | METH_KEYWORDS,
     "Search an indexed adjacency graph without Python callbacks."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_pathfinding",
    .m_doc = module_doc,
    .m_size = -1,
    .m_methods = methods,
};

PyMODINIT_FUNC PyInit__pathfinding(void) {
    PyObject *created = PyModule_Create(&module);
    if (created == NULL) {
        return NULL;
    }
    if (PyModule_AddStringConstant(created,
                                   "__dependency_release__",
                                   ATRINIK_PATHFINDING_RELEASE) < 0) {
        Py_DECREF(created);
        return NULL;
    }
    if (PyModule_AddStringConstant(created,
                                   "__dependency_sha256__",
                                   ATRINIK_PATHFINDING_SHA256) < 0) {
        Py_DECREF(created);
        return NULL;
    }
    return created;
}
