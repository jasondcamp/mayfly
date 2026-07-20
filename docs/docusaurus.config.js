// @ts-check
const {themes: prismThemes} = require('prism-react-renderer');

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'mayfly',
  tagline: 'Short lived ephemeral environments on Kubernetes',
  favicon: 'img/favicon.png',

  url: process.env.DOCS_URL ?? 'https://docs.mayfly.sh',
  baseUrl: process.env.DOCS_BASE_URL ?? '/',
  organizationName: 'jasondcamp',
  projectName: 'mayfly',

  onBrokenLinks: 'throw',
  markdown: {hooks: {onBrokenMarkdownLinks: 'throw'}},

  i18n: {defaultLocale: 'en', locales: ['en']},

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          routeBasePath: '/', // docs-only site
          sidebarPath: './sidebars.js',
          editUrl: 'https://github.com/jasondcamp/mayfly/tree/main/docs/',
        },
        blog: false,
        theme: {customCss: './src/css/custom.css'},
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      colorMode: {
        defaultMode: 'dark',
        respectPrefersColorScheme: true,
      },
      navbar: {
        title: 'mayfly',
        logo: {alt: 'mayfly', src: 'img/mascot.png'},
        items: [
          {
            href: 'https://github.com/jasondcamp/mayfly',
            label: 'GitHub',
            position: 'right',
          },
          {
            href: 'https://pypi.org/project/mayfly-cli/',
            label: 'PyPI',
            position: 'right',
          },
        ],
      },
      footer: {
        style: 'dark',
        copyright: `mayfly — AGPL-3.0. Ephemeral by design.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
        additionalLanguages: ['bash', 'yaml'],
      },
    }),
};

module.exports = config;
