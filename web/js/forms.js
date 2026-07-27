/*
 * Schema driven forms and tables.
 *
 * Every telephony object the appliance holds is described by one schema, which
 * the appliance itself validates against. This file turns that schema into the
 * interface: the table that lists the objects, the form that creates one, and
 * the form that edits one.
 *
 * Nothing here knows what an extension is. Adding a field to the schema on the
 * appliance makes it appear here, validated the same way, without a line
 * changing in the browser. That is what keeps the promise that every operation
 * can be performed from the interface true as the product grows, rather than
 * true only on the day it was written.
 */

(function (root) {
    'use strict';

    var numerals = root.ApplianceNumerals;

    function element(tag, className, text) {
        var node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (text !== undefined && text !== null) {
            node.textContent = String(text);
        }
        return node;
    }

    function clear(node) {
        while (node && node.firstChild) {
            node.removeChild(node.firstChild);
        }
    }

    /* Render a stored value for display in a table cell. */
    function present(field, value) {
        if (value === undefined || value === null || value === '') {
            return field.kind === 'secret' ? 'not set' : '—';
        }
        if (field.kind === 'boolean') {
            return value ? 'yes' : 'no';
        }
        if (field.kind === 'number') {
            return numerals.spellInteger(parseInt(value, 10));
        }
        if (field.kind === 'list') {
            return (Array.isArray(value) ? value : [value])
                .map(function (item) { return numerals.sanitize(String(item)); })
                .join(', ');
        }
        return numerals.sanitize(String(value));
    }

    /* Columns worth showing in the list. A secret is shown only as whether it
     * has been set, never as itself. */
    function columnsFor(spec) {
        return spec.fields.filter(function (field) {
            return field.kind !== 'secret' || true;
        }).slice(0, 6);
    }

    function buildTable(spec, records, handlers) {
        var table = element('table', 'grid');
        var head = element('thead');
        var headRow = element('tr');
        var columns = columnsFor(spec);

        columns.forEach(function (field) {
            headRow.appendChild(element('th', null, field.label));
        });
        headRow.appendChild(element('th', null, 'actions'));
        head.appendChild(headRow);
        table.appendChild(head);

        var body = element('tbody');
        if (!records.length) {
            var emptyRow = element('tr', 'empty');
            var emptyCell = element('td', null, 'no ' + spec.plural + ' are configured yet');
            emptyCell.setAttribute('colspan', String(columns.length + 1));
            emptyRow.appendChild(emptyCell);
            body.appendChild(emptyRow);
        }

        records.forEach(function (record) {
            var row = element('tr');
            columns.forEach(function (field) {
                var value = field.kind === 'secret'
                    ? (record[field.name + '_configured'] ? 'set' : 'not set')
                    : present(field, record[field.name]);
                row.appendChild(element('td', null, value));
            });

            var actions = element('td');
            var edit = element('button', 'secondary', 'edit');
            edit.type = 'button';
            edit.addEventListener('click', function () {
                handlers.edit(record);
            });
            actions.appendChild(edit);

            var remove = element('button', 'caution', 'delete');
            remove.type = 'button';
            remove.addEventListener('click', function () {
                handlers.remove(record);
            });
            actions.appendChild(remove);

            row.appendChild(actions);
            body.appendChild(row);
        });

        table.appendChild(body);
        return table;
    }

    /* Build the input for one field, honouring its kind and its choices. */
    function buildInput(field, value, references) {
        var input;

        if (field.kind === 'boolean') {
            input = element('input');
            input.type = 'checkbox';
            input.checked = value === undefined || value === null ? Boolean(field.default) : Boolean(value);
        } else if (field.kind === 'choice' || (field.references && field.kind !== 'list')) {
            input = element('select');
            var options = field.choices && field.choices.length
                ? field.choices
                : (references[field.references] || []);
            if (!field.required) {
                input.appendChild(element('option', null, ''));
            }
            options.forEach(function (choice) {
                var option = element('option', null, numerals.sanitize(String(choice)));
                option.value = String(choice);
                input.appendChild(option);
            });
            input.value = value === undefined || value === null ? (field.default || '') : String(value);
        } else if (field.kind === 'list') {
            input = element('select');
            input.multiple = true;
            input.size = 5;
            var listOptions = field.choices && field.choices.length
                ? field.choices
                : (references[field.references] || []);
            var chosen = (Array.isArray(value) ? value : (field.default || [])).map(String);
            listOptions.forEach(function (choice) {
                var option = element('option', null, numerals.sanitize(String(choice)));
                option.value = String(choice);
                option.selected = chosen.indexOf(String(choice)) !== -1;
                input.appendChild(option);
            });
        } else {
            input = element('input');
            input.type = field.kind === 'secret' ? 'password'
                : field.kind === 'time' ? 'time'
                : 'text';
            if (field.kind === 'secret') {
                input.autocomplete = 'new-password';
                input.placeholder = 'leave blank to keep the current password';
            }
            input.value = value === undefined || value === null
                ? (field.default === undefined || field.default === null ? '' : String(field.default))
                : String(value);
        }

        input.id = 'field-' + field.name;
        input.name = field.name;
        if (field.required && field.kind !== 'boolean' && field.kind !== 'secret') {
            input.required = true;
        }
        return input;
    }

    function buildForm(spec, record, references) {
        var editing = Boolean(record);
        var form = element('form', 'entity-form');
        form.autocomplete = 'off';

        var heading = element('h3', null,
            (editing ? 'edit the ' : 'add a ') + spec.singular);
        form.appendChild(heading);

        spec.fields.forEach(function (field) {
            var wrapper = element('div', 'field');

            var label = element('label', null, field.label);
            label.setAttribute('for', 'field-' + field.name);
            wrapper.appendChild(label);

            var value = record ? record[field.name] : undefined;
            var input = buildInput(field, value, references || {});
            wrapper.appendChild(input);

            if (field.help) {
                wrapper.appendChild(element('p', 'field-help', field.help));
            }
            wrapper.appendChild(element('p', 'field-error'));

            form.appendChild(wrapper);
        });

        var actions = element('div', 'actions');
        var submit = element('button', null, editing ? 'save the changes' : 'add it');
        submit.type = 'submit';
        actions.appendChild(submit);

        var cancel = element('button', 'secondary', 'cancel');
        cancel.type = 'button';
        cancel.dataset.role = 'cancel';
        actions.appendChild(cancel);

        form.appendChild(actions);
        return form;
    }

    function readForm(form, spec) {
        var values = {};
        spec.fields.forEach(function (field) {
            var input = form.querySelector('[name="' + field.name + '"]');
            if (!input) {
                return;
            }
            if (field.kind === 'boolean') {
                values[field.name] = input.checked;
            } else if (field.kind === 'list') {
                values[field.name] = Array.prototype.slice
                    .call(input.selectedOptions || [])
                    .map(function (option) { return option.value; });
            } else if (field.kind === 'secret') {
                // An untouched password field means "leave it as it was", so
                // it is omitted rather than sent as an empty value.
                if (input.value) {
                    values[field.name] = input.value;
                }
            } else {
                values[field.name] = input.value;
            }
        });
        return values;
    }

    function showErrors(form, errors) {
        Array.prototype.forEach.call(
            form.querySelectorAll('.field-error'),
            function (node) { node.textContent = ''; }
        );
        Array.prototype.forEach.call(
            form.querySelectorAll('.field'),
            function (node) { node.classList.remove('has-error'); }
        );

        Object.keys(errors || {}).forEach(function (name) {
            var input = form.querySelector('[name="' + name + '"]');
            if (!input || !input.parentNode) {
                return;
            }
            input.parentNode.classList.add('has-error');
            var slot = input.parentNode.querySelector('.field-error');
            if (slot) {
                slot.textContent = numerals.sanitize(String(errors[name]));
            }
        });
    }

    root.ApplianceForms = {
        element: element,
        clear: clear,
        present: present,
        buildTable: buildTable,
        buildForm: buildForm,
        readForm: readForm,
        showErrors: showErrors
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = root.ApplianceForms;
    }
}(typeof globalThis !== 'undefined' ? globalThis : this));
