package com.hmwagner.techmanual

import android.app.Application
import com.hmwagner.techmanual.network.ApiClient

class TechManualApp : Application() {
    override fun onCreate() {
        super.onCreate()
        ApiClient.init(this)
    }
}
