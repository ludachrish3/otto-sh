// The traffic generators, one VM per site. West has none yet.
{
  'east-tgen': {
    ip: '10.1.0.100',
    site: 'east',
    is_virtual: true,
    groups: ['tgen'],
    creds: [{ login: 'tester', password: 'tester' }],
  },
}
