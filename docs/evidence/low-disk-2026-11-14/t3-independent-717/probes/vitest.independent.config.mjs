import { defineConfig } from 'vitest/config';
export default defineConfig({root:'/tmp/low-disk-t3-independent-20261114/probes',cacheDir:'/tmp/low-disk-t3-independent-20261114/runs/vite-cache',esbuild:{jsx:'automatic'},test:{environment:'jsdom',include:['app-independent.test.tsx'],maxWorkers:1,fileParallelism:false,environmentOptions:{jsdom:{url:'http://127.0.0.1/'}}}});
