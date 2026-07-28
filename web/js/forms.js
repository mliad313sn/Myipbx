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

    /* Render a stored value for display in a table cell.
     *
     * The spelling rule turns quantities into words. A field the schema marks
     * as an identifier is not a quantity: it is dialled, matched, or read out
     * digit by digit, and spelling it destroys it. An operator handed "two
     * billion fifteen million five hundred fifty thousand one hundred" cannot
     * dial it back, and "two hundred forty-one" is not an extension the way
     * 241 is. The schema is the only place that knows which is which, so it is
     * the schema that decides here. */
    function present(field, value) {
        if (value === undefined || value === null || value === '') {
            return field.kind === 'secret' ? 'not set' : '—';
        }
        if (field.kind === 'boolean') {
            return value ? 'yes' : 'no';
        }
        if (field.identifier) {
            return (Array.isArray(value) ? value : [value]).map(String).join(', ');
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

            var actions = element('td', 'actions-cell');
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

        /* A table of seven columns cannot be narrowed to a telephone, and
         * trying makes the whole page scroll sideways: the navigation, the
         * heading, and every other panel move too, which is the failure a
         * person with a small screen or a large magnification actually feels.
         * The table scrolls inside its own box instead, so the page does not.
         * The box is focusable and named, because a region that scrolls but
         * cannot be reached from the keyboard has only moved the problem. */
        var scroller = element('div', 'table-scroll');
        scroller.setAttribute('tabindex', '0');
        scroller.setAttribute('role', 'region');
        scroller.setAttribute('aria-label', spec.plural);
        scroller.appendChild(table);
        return scroller;
    }

    /* Build the input for one field, honouring its kind and its choices. */
    function buildInput(spec, field, value, references) {
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
                var option = element('option', null,
                    field.identifier ? String(choice) : numerals.sanitize(String(choice)));
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
                var option = element('option', null,
                    field.identifier ? String(choice) : numerals.sanitize(String(choice)));
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

        input.id = inputId(spec, field);
        input.name = field.name;
        if (field.required && field.kind !== 'boolean' && field.kind !== 'secret') {
            input.required = true;
        }
        return input;
    }

    /* Every view keeps its form holder in the document, so a plain
     * "field-name" would exist four times over the moment two forms were left
     * open. A duplicated id breaks the label association silently: the browser
     * hands the screen reader whichever came first, and a person editing a
     * queue is read the label of an extension. The entity kind disambiguates
     * it, because only one form per kind can be open. */
    function inputId(spec, field) {
        return 'field-' + spec.kind + '-' + field.name;
    }

    /* "add a extension" is what this console said, on every form, for as long
     * as the forms have existed. The rule below is the simple one, which is
     * right for every name the schema actually carries -- extension, inbound
     * route, outbound route on one side; trunk, queue, menu on the other. It
     * is spelling, not phonetics: a name beginning with a silent letter or a
     * sounded "u" would need more, and there is none. */
    function article(word) {
        return /^[aeiou]/i.test(String(word || '')) ? 'add an' : 'add a';
    }

    function buildForm(spec, record, references) {
        var editing = Boolean(record);
        var form = element('form', 'entity-form');
        form.autocomplete = 'off';
        form.noValidate = true;

        var headingId = 'form-heading-' + spec.kind;
        var heading = element('h3', null,
            (editing ? 'edit the ' : article(spec.singular) + ' ') + spec.singular);
        heading.id = headingId;
        form.setAttribute('aria-labelledby', headingId);
        form.appendChild(heading);

        /* Where a refusal is announced. It is in the document from the start
         * and empty, because a live region inserted at the moment it has
         * something to say is frequently not announced at all. */
        var summary = element('div', 'form-error-summary');
        summary.id = 'form-summary-' + spec.kind;
        summary.setAttribute('role', 'alert');
        summary.setAttribute('tabindex', '-1');
        summary.hidden = true;
        form.appendChild(summary);

        spec.fields.forEach(function (field) {
            var wrapper = element('div', 'field');
            var identity = inputId(spec, field);

            var label = element('label', null, field.label);
            label.setAttribute('for', identity);
            wrapper.appendChild(label);

            var value = record ? record[field.name] : undefined;
            var input = buildInput(spec, field, value, references || {});
            wrapper.appendChild(input);

            var described = [];
            if (field.help) {
                var help = element('p', 'field-help', field.help);
                help.id = identity + '-help';
                described.push(help.id);
                wrapper.appendChild(help);
            }

            /* The error slot is described from the start rather than only once
             * it fills. A screen reader reads the description at focus, and an
             * empty node contributes nothing, so an association made now costs
             * nothing and one made later is often missed. */
            var error = element('p', 'field-error');
            error.id = identity + '-error';
            described.push(error.id);
            wrapper.appendChild(error);

            input.setAttribute('aria-describedby', described.join(' '));
            if (field.required && field.kind !== 'boolean' && field.kind !== 'secret') {
                input.setAttribute('aria-required', 'true');
            }

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

    /* Mark, describe, announce, and land on it.
     *
     * A refusal has to reach four different people. Someone who can see the
     * form needs the field tinted and the reason under it. Someone using a
     * screen reader needs the field to say it is invalid and to carry the
     * reason as its description, or the two never meet. Someone who cannot see
     * the form at all needs the refusal announced, which is what the summary
     * region is for. And someone driving by keyboard needs to be put on the
     * first bad field rather than left at the submit button, hunting upwards
     * through a form they cannot scan. */
    function showErrors(form, errors) {
        Array.prototype.forEach.call(
            form.querySelectorAll('.field-error'),
            function (node) { node.textContent = ''; }
        );
        Array.prototype.forEach.call(
            form.querySelectorAll('.field'),
            function (node) { node.classList.remove('has-error'); }
        );
        Array.prototype.forEach.call(
            form.querySelectorAll('[aria-invalid]'),
            function (node) { node.removeAttribute('aria-invalid'); }
        );

        var summary = form.querySelector('.form-error-summary');
        var names = Object.keys(errors || {});
        var first = null;
        var reasons = [];

        names.forEach(function (name) {
            var input = form.querySelector('[name="' + name + '"]');
            if (!input || !input.parentNode) {
                return;
            }
            input.parentNode.classList.add('has-error');
            input.setAttribute('aria-invalid', 'true');
            var slot = input.parentNode.querySelector('.field-error');
            var reason = numerals.sanitize(String(errors[name]));
            if (slot) {
                slot.textContent = reason;
                /* The association is already declared by buildForm. It is
                 * asserted again here because a form can be handed to this
                 * function after being built elsewhere, and a described-by
                 * that does not name the error slot is the whole defect. */
                var described = (input.getAttribute('aria-describedby') || '').split(/\s+/);
                if (slot.id && described.indexOf(slot.id) === -1) {
                    described.push(slot.id);
                    input.setAttribute('aria-describedby', described.join(' ').trim());
                }
            }
            var label = input.parentNode.querySelector('label');
            reasons.push((label ? label.textContent + ': ' : '') + reason);
            if (!first) {
                first = input;
            }
        });

        if (summary) {
            clear(summary);
            if (reasons.length) {
                summary.hidden = false;
                summary.appendChild(element('p', null,
                    reasons.length === 1
                        ? 'one field was not accepted.'
                        : numerals.spellInteger(reasons.length) + ' fields were not accepted.'));
                var list = element('ul');
                reasons.forEach(function (reason) {
                    list.appendChild(element('li', null, reason));
                });
                summary.appendChild(list);
            } else {
                summary.hidden = true;
            }
        }

        if (first && typeof first.focus === 'function') {
            first.focus();
        }
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
