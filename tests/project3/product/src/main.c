/* Throwaway host application — exists only so the Zephyr build has an app to
 * attach the `cov_ext` LLEXT target to. The coverage extension (cov_ext.llext)
 * is the real artifact; this base app is never run. */
int main(void) { return 0; }
