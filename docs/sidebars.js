// @ts-check

/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  docs: [
    'intro',
    'getting-started',
    {
      type: 'category',
      label: 'Spec reference',
      collapsed: false,
      items: ['spec/environment', 'spec/services', 'spec/apps'],
    },
    {
      type: 'category',
      label: 'Guides',
      collapsed: false,
      items: [
        'guides/internal-albs',
        'guides/dragonfly',
        'guides/caddis',
        'guides/testing',
        'guides/images',
      ],
    },
    'architecture',
  ],
};

module.exports = sidebars;
