// Host definitions the layouts share: each takes the inventory record and
// returns the otto-owned fields of its host entry.
{
  routerDut(record): { os_type: 'router' },
  linuxTgen(record): { os_type: 'unix', valid_terms: ['ssh'] },
}
