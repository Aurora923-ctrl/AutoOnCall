module.exports = [
    {
        files: ['static/app.js', 'static/js/**/*.js', 'frontend-tests/**/*.js'],
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: 'script',
            globals: {
                AbortController: 'readonly',
                Blob: 'readonly',
                console: 'readonly',
                document: 'readonly',
                EventSource: 'readonly',
                fetch: 'readonly',
                FormData: 'readonly',
                CustomEvent: 'readonly',
                TextDecoder: 'readonly',
                localStorage: 'readonly',
                marked: 'readonly',
                hljs: 'readonly',
                sessionStorage: 'readonly',
                setInterval: 'readonly',
                clearInterval: 'readonly',
                setTimeout: 'readonly',
                clearTimeout: 'readonly',
                URL: 'readonly',
                URLSearchParams: 'readonly',
                window: 'readonly'
            }
        },
        rules: {
            'no-undef': 'error',
            'no-unreachable': 'error',
            'no-dupe-keys': 'error',
            'no-redeclare': 'error'
        }
    },
    {
        files: ['static/js/app_bootstrap.js'],
        languageOptions: {
            globals: {
                AutoOnCallApp: 'readonly'
            }
        }
    },
    {
        files: ['frontend-tests/**/*.js'],
        languageOptions: {
            globals: {
                __dirname: 'readonly',
                require: 'readonly'
            }
        }
    }
];
