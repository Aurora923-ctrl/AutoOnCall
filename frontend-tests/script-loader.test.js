const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('frontend loader preserves the declared script order', () => {
    const appended = [];
    const context = {
        console,
        window: { AUTOONCALL_STATIC_PREFIX: '/static' },
        document: {
            createElement() {
                return {};
            },
            body: {
                appendChild(script) {
                    appended.push(script.src);
                    if (script.onload) script.onload();
                }
            }
        }
    };
    const source = fs.readFileSync(
        path.join(__dirname, '..', 'static', 'app.js'),
        'utf8'
    );

    vm.runInNewContext(source, context);

    assert.deepEqual(
        appended,
        Array.from(
            context.window.AUTOONCALL_SCRIPT_FILES,
            (name) => `/static/js/${name}`
        )
    );
});
