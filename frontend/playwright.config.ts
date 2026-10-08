import { defineConfig } from '@playwright/test';
export default defineConfig({testDir:'./tests',timeout:30000,workers:1,use:{baseURL:'http://127.0.0.1:8001',headless:true,viewport:{width:1440,height:1000},screenshot:'only-on-failure'},webServer:{command:'../.venv/bin/python ../tests/e2e_server.py',url:'http://127.0.0.1:8001/api/health',reuseExistingServer:false,timeout:15000}});
