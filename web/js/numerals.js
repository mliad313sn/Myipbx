/*
 * Numeral spelling subsystem, browser side.
 *
 * This is the twin of appliance/numerals.py and is required to produce
 * identical output for identical input.  The agreement is enforced by
 * tests/test_numerals.py, which executes this file and compares the two
 * implementations across the full range the product can produce.
 *
 * Written as a plain script with no module system and no build step, because
 * the dashboard is served as vanilla markup and scripting from an appliance
 * that may never see a package index.
 */

(function (root) {
    'use strict';

    var SMALL = [
        'zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven',
        'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen',
        'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen'
    ];

    var TENS = [
        '', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy',
        'eighty', 'ninety'
    ];

    var SCALES = [
        '', 'thousand', 'million', 'billion', 'trillion', 'quadrillion',
        'quintillion', 'sextillion'
    ];

    var ORDINAL_SMALL = {
        'zero': 'zeroth', 'one': 'first', 'two': 'second', 'three': 'third',
        'four': 'fourth', 'five': 'fifth', 'six': 'sixth', 'seven': 'seventh',
        'eight': 'eighth', 'nine': 'ninth', 'ten': 'tenth',
        'eleven': 'eleventh', 'twelve': 'twelfth', 'thirteen': 'thirteenth',
        'fourteen': 'fourteenth', 'fifteen': 'fifteenth',
        'sixteen': 'sixteenth', 'seventeen': 'seventeenth',
        'eighteen': 'eighteenth', 'nineteen': 'nineteenth',
        'twenty': 'twentieth', 'thirty': 'thirtieth', 'forty': 'fortieth',
        'fifty': 'fiftieth', 'sixty': 'sixtieth', 'seventy': 'seventieth',
        'eighty': 'eightieth', 'ninety': 'ninetieth', 'hundred': 'hundredth',
        'thousand': 'thousandth', 'million': 'millionth',
        'billion': 'billionth', 'trillion': 'trillionth',
        'quadrillion': 'quadrillionth', 'quintillion': 'quintillionth',
        'sextillion': 'sextillionth'
    };

    var GROUP = 1000;

    function spellGroup(value) {
        var words = [];
        if (value >= 100) {
            words.push(SMALL[Math.floor(value / 100)]);
            words.push('hundred');
            value = value % 100;
        }
        if (value >= 20) {
            var tensWord = TENS[Math.floor(value / 10)];
            var remainder = value % 10;
            words.push(remainder ? tensWord + '-' + SMALL[remainder] : tensWord);
        } else if (value > 0) {
            words.push(SMALL[value]);
        }
        return words;
    }

    function spellInteger(value) {
        if (typeof value !== 'number' || !isFinite(value) || Math.floor(value) !== value) {
            throw new Error('an exact integer is required');
        }
        if (value === 0) {
            return SMALL[0];
        }

        var negative = value < 0;
        var magnitude = Math.abs(value);

        // Grouping uses integer arithmetic so that values stay exact within
        // the safe integer range the dashboard actually produces.
        var groups = [];
        while (magnitude > 0) {
            groups.push(magnitude % GROUP);
            magnitude = Math.floor(magnitude / GROUP);
        }
        if (groups.length > SCALES.length) {
            throw new Error('the value exceeds the largest representable magnitude');
        }

        var words = [];
        for (var index = groups.length - 1; index >= 0; index -= 1) {
            if (groups[index] === 0) {
                continue;
            }
            words = words.concat(spellGroup(groups[index]));
            if (index) {
                words.push(SCALES[index]);
            }
        }

        var rendered = words.join(' ');
        return negative ? 'negative ' + rendered : rendered;
    }

    function spellDecimal(value, places) {
        if (places === undefined) {
            places = 2;
        }
        if (typeof value !== 'number' || !isFinite(value)) {
            throw new Error('a finite real number is required');
        }

        var negative = value < 0;
        var text = Math.abs(value).toFixed(places);
        var split = text.split('.');
        var wholeText = split[0];
        var fractionText = (split[1] || '').replace(/0+$/, '');

        var words = spellInteger(parseInt(wholeText, 10));
        if (fractionText) {
            var spoken = [];
            for (var index = 0; index < fractionText.length; index += 1) {
                spoken.push(SMALL[parseInt(fractionText.charAt(index), 10)]);
            }
            words = words + ' point ' + spoken.join(' ');
        }
        if (negative && (parseInt(wholeText, 10) !== 0 || fractionText)) {
            return 'negative ' + words;
        }
        return words;
    }

    function spellOrdinal(value) {
        var cardinal = spellInteger(value);
        var boundary = cardinal.lastIndexOf(' ');
        var head = boundary === -1 ? '' : cardinal.slice(0, boundary);
        var tail = boundary === -1 ? cardinal : cardinal.slice(boundary + 1);

        if (tail.indexOf('-') !== -1) {
            var halves = tail.split('-');
            tail = halves[0] + '-' + ORDINAL_SMALL[halves[1]];
        } else {
            tail = ORDINAL_SMALL[tail];
        }
        return head ? head + ' ' + tail : tail;
    }

    function spellDuration(seconds) {
        if (typeof seconds !== 'number' || Math.floor(seconds) !== seconds) {
            throw new Error('an exact second count is required');
        }
        if (seconds < 0) {
            throw new Error('a duration cannot be negative');
        }
        if (seconds === 0) {
            return 'zero seconds';
        }

        var units = [
            [86400, 'day', 'days'],
            [3600, 'hour', 'hours'],
            [60, 'minute', 'minutes'],
            [1, 'second', 'seconds']
        ];

        var remaining = seconds;
        var parts = [];
        for (var index = 0; index < units.length; index += 1) {
            var unitSeconds = units[index][0];
            var count = Math.floor(remaining / unitSeconds);
            remaining = remaining % unitSeconds;
            if (count) {
                var name = count === 1 ? units[index][1] : units[index][2];
                parts.push(spellInteger(count) + ' ' + name);
            }
        }
        return parts.join(' ');
    }


    /* Shapes that are identifiers rather than quantities, and are left alone.
     *
     * The rule is the difference between a number an operator reads and a
     * number an operator uses.  "Twelve active calls" is a quantity and reads
     * better spelled.  An address, a port, a version, a device name or a
     * response code is an identifier: the operator types it, compares it
     * against a label on a cable, or searches for it, and spelling it does not
     * make it clearer, it makes it unusable.
     *
     * This list is held identical to the one in the control plane's numeral
     * module, and a test asserts the two agree for every value the product can
     * produce. */
    var IDENTIFIER_SHAPES = [
        /\b(?:\d{1,3}\.){3}\d{1,3}\/\d{1,2}\b/g,
        /\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b/g,
        /\b(?:\d{1,3}\.){3}\d{1,3}\b/g,
        /\b(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}\b/g,
        /\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b/g,
        /\b[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]\b/g,
        /\b[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\b/g,
        /\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?(?!\d)/g,
        /\b\d{2}:\d{2}(?::\d{2})?(?!\d)/g,
        /\bv?\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.]+)?\b/g,
        /\b(?:eth|en[a-z0-9]*|wl[a-z0-9]*|lo|tty[A-Za-z]*|sd[a-z]|nvme|dahdi|span|zap)\d+\b/g,
        /(?:\/[A-Za-z0-9._-]*\d[A-Za-z0-9._-]*)+/g,
        /\b(?:SIP|HTTP|status|code|error)\s+\d{3}\b/gi,
        /\b(?:chmod|mode|permissions)\s+[0-7]{3,4}\b/gi,
        /\b(?:errno|error\s+number)\s+\d+\b/gi,
        /\(\s*'[^']*'\s*,\s*\d{1,5}\s*\)/g,
        /\bport(?:\s+number)?\s+\d{1,5}\b/gi,
        /\b(?:TDM|TE|AEX|HA|HB|B)\d+[A-Z]?\b/g,
        /\bextension\s+\d+\b/gi
    ];

    /* A marker that cannot occur in real text, holding an identifier's place
     * while the quantities around it are spelled.  The index inside it is
     * written in letters rather than digits, because the marker passes through
     * the very function that turns digits into words and a numeric index would
     * be spelled along with everything else. */
    var GUARD_OPEN = '\uE000';
    var GUARD_CLOSE = '\uE001';
    var GUARD_FINDER = /\uE000([a-z]+)\uE001/g;

    function guardLabel(index) {
        var label = '';
        var remaining = index + 1;
        while (remaining > 0) {
            var remainder = (remaining - 1) % 26;
            remaining = Math.floor((remaining - 1) / 26);
            label = String.fromCharCode(97 + remainder) + label;
        }
        return label;
    }

    function guardIndex(label) {
        var index = 0;
        for (var position = 0; position < label.length; position += 1) {
            index = index * 26 + (label.charCodeAt(position) - 96);
        }
        return index - 1;
    }

    function identifierSpans(text) {
        var found = [];
        var shape;
        var match;
        for (var i = 0; i < IDENTIFIER_SHAPES.length; i += 1) {
            shape = IDENTIFIER_SHAPES[i];
            shape.lastIndex = 0;
            while ((match = shape.exec(text)) !== null) {
                if (match[0].length === 0) {
                    shape.lastIndex += 1;
                    continue;
                }
                found.push([match.index, match.index + match[0].length]);
            }
        }
        found.sort(function (left, right) {
            return left[0] - right[0] || (right[1] - right[0]) - (left[1] - left[0]);
        });
        var merged = [];
        for (var j = 0; j < found.length; j += 1) {
            if (merged.length && found[j][0] < merged[merged.length - 1][1]) {
                if (found[j][1] > merged[merged.length - 1][1]) {
                    merged[merged.length - 1][1] = found[j][1];
                }
                continue;
            }
            merged.push([found[j][0], found[j][1]]);
        }
        return merged;
    }

    function sanitize(text, spellIdentifiers) {
        if (typeof text !== 'string') {
            text = String(text);
        }
        if (!/[0-9]/.test(text)) {
            return text;
        }
        if (!spellIdentifiers) {
            var spans = identifierSpans(text);
            if (spans.length) {
                var held = [];
                var rebuilt = [];
                var walked = 0;
                for (var s = 0; s < spans.length; s += 1) {
                    rebuilt.push(text.slice(walked, spans[s][0]));
                    rebuilt.push(GUARD_OPEN + guardLabel(held.length) + GUARD_CLOSE);
                    held.push(text.slice(spans[s][0], spans[s][1]));
                    walked = spans[s][1];
                }
                rebuilt.push(text.slice(walked));
                var spelledText = sanitize(rebuilt.join(''), true);
                GUARD_FINDER.lastIndex = 0;
                return spelledText.replace(GUARD_FINDER, function (whole, label) {
                    return held[guardIndex(label)];
                });
            }
        }

        var pieces = [];
        var cursor = 0;
        var pattern = /[0-9]+/g;
        var match;

        while ((match = pattern.exec(text)) !== null) {
            var start = match.index;
            var end = start + match[0].length;
            pieces.push(text.slice(cursor, start));

            var run = match[0];
            var stripped = run.replace(/^0+/, '');
            var spelled;
            if (stripped) {
                var leading = [];
                for (var index = 0; index < run.length - stripped.length; index += 1) {
                    leading.push('zero');
                }
                try {
                    spelled = leading.concat([spellInteger(parseInt(stripped, 10))]).join(' ');
                } catch (error) {
                    var digits = [];
                    for (var position = 0; position < run.length; position += 1) {
                        digits.push(SMALL[parseInt(run.charAt(position), 10)]);
                    }
                    spelled = digits.join(' ');
                }
            } else {
                var zeros = [];
                for (var zeroIndex = 0; zeroIndex < run.length; zeroIndex += 1) {
                    zeros.push('zero');
                }
                spelled = zeros.join(' ');
            }

            var previous = pieces[pieces.length - 1];
            if (previous && /[A-Za-z]$/.test(previous)) {
                spelled = ' ' + spelled;
            }
            if (end < text.length && /[A-Za-z]/.test(text.charAt(end))) {
                spelled = spelled + ' ';
            }

            pieces.push(spelled);
            cursor = end;
        }

        pieces.push(text.slice(cursor));
        return pieces.join('');
    }

    function containsDigit(text) {
        return /[0-9]/.test(String(text));
    }

    var ApplianceNumerals = {
        spellInteger: spellInteger,
        spellDecimal: spellDecimal,
        spellOrdinal: spellOrdinal,
        spellDuration: spellDuration,
        sanitize: sanitize,
        containsDigit: containsDigit
    };

    /* The dashboard reads this from the global scope, and the cross
     * implementation agreement test loads the very same file through the
     * module system.  Both are served explicitly rather than by leaking a
     * declaration into whatever scope happens to be current. */
    root.ApplianceNumerals = ApplianceNumerals;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = ApplianceNumerals;
    }
}(typeof globalThis !== 'undefined' ? globalThis : this));
